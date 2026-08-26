#!/usr/bin/env python3
"""Auto-resolve merge conflicts — BUNDLE step (the untrusted half of finalize).

Verifies the working tree is fully resolved (no unmerged paths, no stray conflict
markers, no edit outside the conflicted set), completes the merge commit LOCALLY,
and writes it to $BUNDLE_DIR as a git bundle for the separate `land` job.

Everything above this step in the `resolve` job — the PR-head checkout, the local
composites, `pnpm resolve-generated`, the model itself — is PR-authored or
model-authored, and this script runs in the same job. So it pushes nothing and
holds no push credential: its commit is UNTRUSTED OUTPUT that
auto-resolve/land.sh re-derives every property of from git, and this step fails
LOUD rather than bundle a half-resolved tree. A wrong auto-resolution must never
reach the branch.

Env:
  HEAD_REF, BASE_REF, PR, BUNDLE_DIR   required
  CONFLICT_LIST                        the paths the resolver was asked to resolve
  MODIFY_DELETE_PATHS, MODIFY_DELETE_VERDICTS
  SIDECAR_PATHS, SIDECAR_RESOLUTIONS
  DEFERRED_REGEN                       generated paths the regen pre-pass owns
  LLM_PERMISSION_DENIALS, LLM_PERMISSION_DENIED_TOOLS,
  LLM_PERMISSION_DENIALS_BY_FILE       what the fan-out's execution log reported
  CLAUDE_CODE_OAUTH_TOKEN[_FALLBACK…]  presence enables the self-review gate
  RESOLVER_PREFERRED_TOKEN             successful resolve credential, tried first
"""

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# PROBLEM CLASS — a buffered print() and an inherited-stdout subprocess share one fd but not
# one buffer: a print() here can sit unflushed while a child writes through, so the log comes
# back reordered. Line buffering flushes at each newline — except a `print(…, end="")` tail,
# so the two raw-output sites flush by hand. The guard is load-bearing: a harness can swap in
# a capture object with no `reconfigure`, which a cast misses. Import precedes `main()`.
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(
        line_buffering=True
    )  # allow-stdio-swap: one single-threaded CLI process, set once at import before any worker starts

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from _denials import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Denials,
)
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    bind_repo,
    git,
    git_lines,
    git_status,
)
from _hook_gate import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    hook_could_not_run,
    hooks_needing_the_project_env,
)
from _lockfiles import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LockfileError,
    regenerate as regenerate_lockfile,
    rule_for as lockfile_rule_for,
)
from _marker_verdict import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    CONFLICT_MARKER_RE,
    MarkerVerdict,
    declined_files,
    files_with_no_deliverable,
    marker_file_text,
)
from _out_of_conflict import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    MalformedMarkersError,
    MechanicalMergeError,
    PathMissingFromMechanicalTreeError,
    RepairUnsoundError,
    rewrites_outside_conflicts,
)
from _post_merge_check import run as run_post_merge_check  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _credentials import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ordered_oauth_tokens,
)
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    escalation_block,
    fail,
    run_or_refuse,
)
from prompts import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    POST_MERGE_REJECTED,
    REGEN_REJECTED,
)
from _repair_pass import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    RepairPass,
)
from regen_marked_regions import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    resolve_generated_regions,
    unmerged_paths,
)

_SHARED_NAMES = json.loads(
    (_SCRIPT_DIR.parent / "lib" / "shared-names.json").read_text(encoding="utf-8")
)
# The ref this step hands the resolved merge to LAND under. lib.sh reads it from
# the same file, so this step and the shell steps beside it cannot spell it
# differently.
AUTO_RESOLVE_RESULT_REF = _SHARED_NAMES["auto_resolve"]["result_ref"]

# The reviewer's CANNOT-VERIFY status, which is a different report from its
# flagged-the-resolution status. Exit 3 is a third: flagged, with NO fix round
# attempted, because the credential ladder spent the budget.
_SELF_REVIEW_CANNOT_VERIFY = 2
_SELF_REVIEW_FLAGGED_UNATTEMPTED = 3


def untrusted_head() -> bool:
    """Whether this run merged a head the resolve job may not execute — a fork.

    INVARIANT — the hook passes below run the MERGED tree's own pre-commit hooks,
    which on a fork head are code the fork's author wrote. This refusal to run
    them is what keeps a fork's code out of the job holding every model
    credential, and the resolve job installs no hook toolchain for such a run. The
    pull request's own required checks judge the merged bytes instead, which is
    the same argument `verify_merge_carried_content` already makes for the paths
    nobody resolved."""
    return os.environ.get("AUTO_RESOLVE_UNTRUSTED_HEAD") == "true"


# How the CALLING repository re-derives its generated files, from the workflow's
# `pre-pass-command` input, split the way a shell would. Empty is a caller with
# no generators, and never a guess at one: a wrong command reports "nothing to
# re-derive" for files that needed it. A FORK head empties it too — the command
# is a script that head's manifest defines, and this job holds every credential.
PRE_PASS = (
    [] if untrusted_head() else shlex.split(os.environ.get("AUTO_RESOLVE_PRE_PASS", ""))
)


def run_pre_pass(*args: str) -> subprocess.CompletedProcess:
    """The caller's pre-pass with ARGS, output captured."""
    return run_or_refuse(
        [*PRE_PASS, *args],
        label="pre-pass command",
        input_name="pre-pass-command",
        lost="re-derive the generated files",
    )


def env_list(name: str) -> list[str]:
    """A whitespace-separated path list, the way bash's `read -ra` splits one."""
    return os.environ.get(name, "").split()


def is_unmergeable(path: str, base_remote_ref: str) -> bool:
    """A path no edit can resolve: `-merge`-attributed, or binary to git.

    The attribute is read from BASE_REMOTE_REF, not the worktree, matching
    prepare.sh's `is_unmergeable` (lib.sh) — the two must agree on the same
    path, since prepare only sends a path here (in CONFLICT_LIST) after
    classifying it as mergeable. Reading the worktree's `.gitattributes`
    instead would judge PRs whose branch still carries an attribute the base
    already removed, which mismatches prepare's now base-derived verdict."""
    if (
        git("check-attr", f"--source={base_remote_ref}", "merge", "--", path)
        .strip()
        .endswith(": merge: unset")
    ):
        return True
    numstat = git("diff", "--numstat", "HEAD", "MERGE_HEAD", "--", path)
    return numstat.split("\t")[0] == "-" if numstat else False


class Bundle(RepairPass):
    """One run of the step: what the resolver was asked to resolve, what it left
    in the tree, and the state the checks below accumulate."""

    def __init__(self) -> None:
        self.pr = os.environ["PR"]
        self.bundle_dir = Path(os.environ["BUNDLE_DIR"])
        self.allowed = env_list("CONFLICT_LIST")
        self.modify_delete = env_list("MODIFY_DELETE_PATHS")
        self.sidecar = env_list("SIDECAR_PATHS")
        self.deferred = env_list("DEFERRED_REGEN")
        # Lockfiles the resolver's own registry owns, deferred because their
        # manifest was conflicted when prepare ran. A fork head regenerates none
        # of them, for the reason PRE_PASS is empty there.
        self.deferred_lockfiles = (
            [] if untrusted_head() else env_list("DEFERRED_LOCKFILES")
        )
        self.denials = Denials.from_env()
        self.staged: list[str] = []
        self.checked_out_head = ""
        self.merge_base_side = ""
        self.unverified = False
        self.declined: list[str] = []
        self.carried_hook_failures: list[str] = []
        self.out_of_conflict_rewrites: list[str] = []

    def read_parents(self) -> None:
        """The merge's two parents, which the thin bundle below is expressed against.

        Which git names hold them depends on the path `prepare` took, so MERGE_HEAD
        is never read unconditionally."""
        if git_status("rev-parse", "-q", "--verify", "MERGE_HEAD") == 0:
            self.merge_base_side = git("rev-parse", "MERGE_HEAD").strip()
            self.checked_out_head = git("rev-parse", "HEAD").strip()
            return
        # No open merge and no merge commit either: nothing here is a resolution
        # to bundle, so name that rather than letting HEAD^2 die as a bare
        # rev-parse.
        if git_status("rev-parse", "-q", "--verify", "HEAD^2") != 0:
            fail(
                "no merge to bundle: there is no merge in progress and HEAD is "
                "not a merge commit",
                "the resolver job reached the bundle step with neither an "
                "in-progress merge nor a merge commit, so there is nothing to "
                "hand to the land job. That is a defect in this workflow's "
                "plumbing, **not** a hard conflict.",
                resolver_fault=True,
            )
        self.merge_base_side = git("rev-parse", "HEAD^2").strip()
        self.checked_out_head = git("rev-parse", "HEAD^").strip()

    def refuse_edits_outside_the_set(self) -> None:
        """INVARIANT — the resolver may only have touched the files it was asked to
        resolve; any other modified tracked file, or any new untracked file, aborts
        the run. Checked BEFORE staging."""
        unmerged = {line.split("\t")[-1] for line in git_lines("ls-files", "-u")}
        allowed = set(self.allowed)
        for name in git_lines("diff", "--name-only"):
            if name in unmerged or name in allowed:
                continue
            fail(
                f"the resolver modified a file outside the conflicted set ('{name}')",
                "the LLM edited a file it was not asked to touch.",
            )
        if git_lines("ls-files", "--others", "--exclude-standard"):
            fail(
                "the resolver created new untracked files",
                "the LLM added files it was not asked to.",
            )

    def refuse_unmergeable_paths(self) -> None:
        """no unmergeable path (a `-merge`-attributed lockfile, a binary)
        may sit in CONFLICT_LIST; an edit-based resolution of one is unverifiable."""
        base_remote_ref = f"origin/{os.environ['BASE_REF']}"
        for name in self.allowed:
            if lockfile_rule_for(name) is not None:
                fail(
                    f"the recognized lockfile '{name}' reached CONFLICT_LIST",
                    f"`{name}` is a lockfile, so the only correct resolution is "
                    "re-running its lock command against the merged manifest. "
                    "The routing pass should never have handed it to a model.",
                    resolver_fault=True,
                )
            if is_unmergeable(name, base_remote_ref):
                fail(
                    f"unmergeable (lockfile/binary) path '{name}' in CONFLICT_LIST",
                    f"`{name}` cannot be merged textually; resolve it by hand "
                    "(e.g. re-run the lockfile tool after merging).",
                )

    def stage_modify_delete(self) -> None:
        """Modify/delete paths are staged from the resolver's VERDICT, not from the
        working tree, which cannot express the answer.

        No verdict, an unreadable one, or one that is not keep/delete is
        a refusal, never a default."""
        if not self.modify_delete:
            return
        named = " ".join(self.modify_delete)
        path = os.environ.get("MODIFY_DELETE_VERDICTS", "")
        verdicts: Any = None
        if path and Path(path).is_file() and Path(path).stat().st_size:
            try:
                verdicts = json.loads(Path(path).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                verdicts = None
        else:
            fail(
                f"no modify/delete verdict file at '{path or '<unset>'}' for "
                f"path(s) '{named}'",
                f"the merge has modify/delete conflict(s) (`{named}`) and this "
                "run produced no verdict file for them, so nothing decided "
                "whether those files should survive. That is a defect in this "
                "workflow's plumbing, **not** a hard conflict.",
                resolver_fault=True,
            )
        for name in self.modify_delete:
            entry = verdicts.get(name) if isinstance(verdicts, dict) else None
            decision = entry.get("decision") if isinstance(entry, dict) else None
            if decision == "keep":
                git("add", "--", name)
            elif decision == "delete":
                git("rm", "-q", "-f", "--", name)
            elif decision == "decline":
                # A judged refusal, not missing plumbing: say what the model
                # would not decide, which is the whole value of the record.
                said = str(entry.get("reasoning") or "").strip()
                fail(
                    f"the resolver declined the modify/delete path '{name}'",
                    f"`{name}` is a modify/delete conflict — one side removed "
                    "it, the other changed it — and the resolver read it and "
                    "declined to decide."
                    + (f" Its own account: {said}" if said else "")
                    + " Decide it by hand: keeping the file and honouring the "
                    "deletion are both plausible.",
                    escalate=escalation_block(
                        [name],
                        said
                        or "one side deleted this file and the other changed "
                        "it, and nothing in the history says which was meant.",
                    ),
                )
            else:
                fail(
                    "the resolver returned no usable keep-or-delete verdict for "
                    f"the modify/delete path '{name}'",
                    f"`{name}` is a modify/delete conflict — one side removed "
                    "it, the other changed it — and the resolver returned no "
                    "verdict for it at all, not even a decline. Decide it by "
                    "hand: keeping the file and honouring the deletion are both "
                    "plausible, and picking one without a judgement is how a "
                    "deliberate deletion gets silently reverted.",
                    resolver_fault=True,
                )

    def install_sidecar_resolutions(self) -> None:
        """Install a sidecar path's merged file, which its shard wrote to a scratch
        path outside the repository because the resolver may not write there.

        A missing resolution is a refusal, never a fallback."""
        if not self.sidecar:
            return
        named = " ".join(self.sidecar)
        path = os.environ.get("SIDECAR_RESOLUTIONS", "")
        resolutions: Any = None
        if path and Path(path).is_file() and Path(path).stat().st_size:
            try:
                resolutions = json.loads(Path(path).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                resolutions = None
        else:
            fail(
                f"no sidecar resolution file at '{path or '<unset>'}' for "
                f"path(s) '{named}'",
                f"the merge has conflict(s) (`{named}`) the resolver cannot "
                "write in place, and this run produced no file recording what "
                "it resolved them to. That is a defect in this workflow's "
                "plumbing, **not** a hard conflict.",
                resolver_fault=True,
            )
        for name in self.sidecar:
            resolved = resolutions.get(name) if isinstance(resolutions, dict) else None
            source = Path(resolved) if isinstance(resolved, str) and resolved else None
            if source is None or not source.is_file() or not source.stat().st_size:
                # A sidecar shard declines by handing out nothing, so absence
                # alone cannot tell a judgement from a harness fault. The decline
                # record can, and it carries the reasoning the prompt promised
                # would reach this comment.
                said = str(declined_files().get(name, "")).strip()
                where = (
                    f"`{name}` sits where the resolver cannot write in place, so "
                    "it resolves by handing the merged file out to this step."
                )
                if said:
                    fail(
                        f"the resolver declined the sidecar path '{name}'",
                        f"{where} It declined instead, and its own account of "
                        f"what it would not merge is: {said} Resolve this one by "
                        "hand.",
                        escalate=escalation_block([name], said),
                    )
                fail(
                    f"the resolver produced no resolution for the sidecar path '{name}'",
                    f"{where} It handed out nothing and recorded no decline, so "
                    "nothing says whether it judged the conflict or fell over. "
                    "Resolve this one by hand.",
                    resolver_fault=True,
                )
            # The sidecar source is never a symlink; a link planted at
            # the scratch path would copy anything into the repo.
            if source.is_symlink():
                fail(
                    f"the sidecar resolution for '{name}' is a symlink ('{source}')",
                    f"the file the resolver handed out for `{name}` is a "
                    "symbolic link rather than the merged content, so installing "
                    "it would commit whatever it points at. Resolve this one by "
                    "hand.",
                )
            Path(name).write_bytes(source.read_bytes())

    def rederive_generated_regions(self) -> None:
        """The generated-region pass again, over what the resolver left conflicted.

        prepare.sh runs this same pass BEFORE the model, on the tree at its most
        broken: every other conflicted file still holds `<<<<<<<`, and a generator
        that walks the whole tree dies on one of them — `gen_gate_paths_regex.py`
        reaches `ast.parse` over every python file its gate closure names. Its
        region then goes to the model, which on a single 30 KB generated line
        leaves the markers standing, and the run hands off. PR #4350 lost four runs
        that way. Here the siblings carry the model's resolutions, so the generator
        reads a tree that parses and the region resolves with no model at all.

        Ordered before `stage_text_resolutions`, which stages every conflicted path
        whether or not it is resolved: after it nothing is unmerged, and this pass
        reads the unmerged set. A pass that stages nothing changes nothing — the
        marker refusal downstream is the one this run reaches either way."""
        dirty = set(git_lines("diff", "--name-only"))
        staged = resolve_generated_regions(unmerged_paths(), llm_runs_next=False).staged
        # The restore prepare.sh makes after its own run of this pass: a generator
        # rewrites every splice output it OWNS, not only the conflicted one, and a
        # clean sibling left modified here reaches verify_resolved_content's stray
        # check, which blames pre-commit and refuses the run.
        for name in git_lines("diff", "--name-only"):
            if name not in dirty and name not in set(staged):
                git("checkout", "--", name)
        if staged:
            print(
                f"Re-derived {len(staged)} generated-region conflict(s) with their "
                f"own generator after the resolver ran: {' '.join(staged)}"
            )

    def stage_text_resolutions(self) -> None:
        """The remaining conflicted paths, staged from the tree; a modify/delete path
        is excluded because the block above already decided it.

        Named paths, never `git add -A`: that would also stage a still-unmerged
        path git left marker-less and at "ours" — a `-merge`-attributed lockfile,
        a binary — silently committing a wrong "ours" resolution."""
        decided = set(self.modify_delete)
        self.staged = [name for name in self.allowed if name not in decided]
        if self.staged:
            git("add", "--", *self.staged)

    def salvage_declined_paths(self) -> None:
        """Keep the head's content at a path the model DECLINED, so one declined file
        does not discard every other file this run resolved.

        A whole-tree marker check over per-path work is what made a run that resolved
        19 files throw all 19 away because the 20th kept its markers — and the next
        scan then buys the identical resolution again.

        Only a DELIBERATE decline is salvaged, and the shard's own decline RECORD is
        what says a path is one. Every other cause returns untouched for
        :class:`MarkerVerdict` to refuse as it does today: a permission denial means
        the write path was closed, so keeping the head's content would silently drop
        the base's edit over a fixable grant, and a shard that reported success while
        answering nothing is a harness fault with no judgement behind it. Salvaging
        nothing is also a refusal — a run whose every conflicted path declined
        resolved nothing to land."""
        # Deferred paths are excluded for the reason the marker sweep below excludes
        # them: the regen pre-pass has not run yet, so their markers are expected and
        # about to be replaced — declining one would keep a stale generated file.
        # `git grep` exits 1 when nothing matches, which git_lines raises on, so the
        # marker-free run (the common one) is asked about with git_status first.
        pathspec = (".", *(f":(exclude){name}" for name in self.deferred))
        if git_status("grep", "-qE", CONFLICT_MARKER_RE, "--", *pathspec) != 0:
            return
        marker_files = git_lines("grep", "-lE", CONFLICT_MARKER_RE, "--", *pathspec)
        if self.denials.count > 0 or files_with_no_deliverable() & set(marker_files):
            return
        resolvable = set(self.allowed) - set(self.deferred)
        declined = sorted(set(marker_files) & resolvable & set(declined_files()))
        reverts = [
            name for name in declined if self.keeping_head_reverts_the_base(name)
        ]
        if reverts:
            # INVARIANT — refusing here is what stops a decline that UNDOES a landed
            # commit from being salvaged into a pushed merge. These keep their
            # markers, so the leftover-marker verdict below refuses the run and its
            # salvage patch still carries the paths this run did resolve.
            print(
                "::error::the resolver declined "
                f"{marker_file_text(reverts)}, where this branch's content is "
                "byte-identical to the merge base — keeping it would REVERT the "
                "base's landed change rather than choose between two edits."
            )
            declined = [name for name in declined if name not in set(reverts)]
        if not declined or len(declined) == len(resolvable):
            return
        for name in declined:
            git("checkout", self.checked_out_head, "--", name)
            git("add", "--", name)
        self.declined = declined
        self.staged = [name for name in self.staged if name not in set(declined)]
        print(
            "::warning::the resolver declined "
            f"{marker_file_text(declined)}; keeping this branch's content there and "
            "landing the rest. The dropped edit(s) are named on the PR."
        )

    def revert_out_of_conflict_rewrites(self) -> None:
        """A bundled file should only differ from the mechanical merge INSIDE a
        conflict region, because outside a span both parents wrote the same bytes.

        An out-of-span change is REVERTED wherever the revert needs no judgement,
        which is most of them. Where the revert would have to guess, the run REPORTS
        the change and lands it: the alternative costs the PR a handoff and a human
        over hunks that were sound. `land` then names the lines on the PR and turns
        auto-merge off, so the merge-delta reviewer reads them before anyone merges.

        `refuse_edits_outside_the_set` is the same question one level up, over whole
        paths, and still refuses. It cannot see this one: a conflicted file is in the
        set, so a rewrite of its untouched context reads as part of the resolution.

        Deferred paths are excluded because a generator, not the resolver, writes
        them; modify/delete has no text to compare; a declined path keeps the head's
        whole file, which the decline notes report instead."""
        gated = (
            set(self.allowed)
            - set(self.deferred)
            - set(self.modify_delete)
            - set(self.declined)
        )
        if not gated:
            return
        try:
            offenders = rewrites_outside_conflicts(
                self.checked_out_head, self.merge_base_side, sorted(gated)
            )
        except (
            MechanicalMergeError,
            MalformedMarkersError,
            PathMissingFromMechanicalTreeError,
            RepairUnsoundError,
        ) as exc:
            fail(
                f"the mechanical merge comparison failed: {exc}",
                "the resolution could not be compared against the mechanical "
                "merge, so it was not bundled.",
                resolver_fault=True,
            )
        for name, offender in sorted(offenders.items()):
            violations = offender.violations
            shown = ", ".join(v.describe() for v in violations[:5])
            rest = len(violations) - 5
            ranges = f"{shown}, and {rest} more" if rest > 0 else shown
            if offender.repaired is not None:
                # INVARIANT — the bundled file now matches the mechanical merge
                # outside every span, which is what the refusal below demands.
                Path(name).write_text(offender.repaired, encoding="utf-8")
                git("add", "--", name)
                print(
                    f"::warning::reverted the resolution's out-of-conflict "
                    f"change to '{name}' (mechanical line(s) {ranges}): outside a "
                    "span both parents wrote the same bytes, so the mechanical "
                    "merge is the content, and the hunks this run resolved stand."
                )
                continue
            # The revert would have to guess: a changed block covers a span only in
            # part, or undoing it would drop a line the mechanical merge also holds
            # outside every span. The resolution lands as written and `land` reports
            # it, rather than costing the PR a handoff over hunks that were sound.
            self.out_of_conflict_rewrites.append(f"{name}\t{ranges}")
            print(
                "::warning::the resolution rewrote lines outside every conflict "
                f"region in '{name}' (mechanical line(s) {ranges}) and the revert "
                "was ambiguous, so those lines land as written. Read them as "
                "hand-written code: `git -c merge.conflictStyle=merge merge-tree "
                f"--write-tree {self.checked_out_head} {self.merge_base_side}` "
                "writes the mechanical merge those line numbers index, and "
                f"`git show <tree>:{name}` prints it. The pin is part of the "
                "command: under diff3 every span carries a base section, and "
                "every line number below it moves."
            )

    def keeping_head_reverts_the_base(self, name: str) -> bool:
        """Whether keeping this branch's content at `name` undoes a landed commit.

        True when the head's blob equals a merge base's AND the base side's blob
        differs: the head never edited the path, so the base side carries the only
        change and keeping the head's side drops it. `--all` because a criss-cross
        history has several bases and the head may match any one of them. False
        when the path is absent from a base (the head added it), and false when the
        base side matches the head too — there is no landed change to undo."""
        head_blob = self.blob_at(self.checked_out_head, name)
        if not head_blob or self.blob_at(self.merge_base_side, name) == head_blob:
            return False
        bases = git_lines(
            "merge-base", "--all", self.checked_out_head, self.merge_base_side
        )
        return any(self.blob_at(base, name) == head_blob for base in bases)

    def blob_at(self, ref: str, name: str) -> str:
        """The blob id `ref` records for `name`, empty when it records none."""
        return git("rev-parse", "-q", "--verify", f"{ref}:{name}", check=False).strip()

    def marker_verdict(self) -> MarkerVerdict:
        """The leftover-marker refusal (_marker_verdict.py), bound to this
        run's state at the moment it is asked for — after `read_parents`, so
        the salvage patch diffs against the parents this run actually merged."""
        return MarkerVerdict(
            allowed=self.allowed,
            denials=self.denials,
            pr=self.pr,
            bundle_dir=self.bundle_dir,
            checked_out_head=self.checked_out_head,
            merge_base_side=self.merge_base_side,
        )

    def run_deferred_regeneration(self) -> None:
        """Re-derive the generated outputs whose sources the LLM resolved — a
        whole rule-owned file, and a `BEGIN GENERATED` region inside a
        hand-written one.

        A still-unmerged deferred path and a non-zero exit from either pass both
        abort, so a half-derived tree is never bundled."""
        self.regenerate_deferred_lockfiles()
        if not self.deferred:
            return
        if not PRE_PASS:
            # A path reached this list because prepare.sh recognised it as
            # generated, so the caller HAS derived files and declared no command
            # that re-derives them. Bundling would ship whatever the model wrote
            # into a file no build produces.
            named = " ".join(sorted(self.deferred))
            fail(
                f"generated file(s) '{named}' deferred with no pre-pass command",
                f"the generated file(s) `{named}` need re-deriving after this "
                "resolution, and the workflow was called with no "
                "`pre-pass-command` to re-derive them with.",
                resolver_fault=True,
            )
        rederive, region = self._rederive()
        # A generator reads the merged SOURCES as a program, so it dies on a file
        # git text-merged into something that does not run — a name one side
        # renamed and the other still calls. That is the repair pass's own defect
        # class, so the tree gets one before this hands the conflict to a human.
        if rederive.returncode or region.returncode or self._deferred_unmerged():
            handle, name = tempfile.mkstemp()
            os.close(handle)
            report = Path(name)
            report.write_text(
                rederive.stdout + rederive.stderr + region.stdout + region.stderr,
                encoding="utf-8",
            )
            if self.repair_merged_tree(report, REGEN_REJECTED):
                rederive, region = self._rederive()
        still_unmerged = self._deferred_unmerged()
        if still_unmerged:
            named = " ".join(still_unmerged)
            fail(
                f"deferred generated file(s) did not regenerate cleanly ('{named}')",
                f"the generated file(s) `{named}` could not be regenerated from "
                "the resolved sources.",
            )
        if rederive.returncode != 0:
            fail(
                f"the deferred re-derivation pre-pass exited {rederive.returncode}",
                "re-deriving the generated file(s)/lockfile(s) after the conflict "
                "resolution failed.",
            )
        if region.returncode != 0:
            fail(
                f"the deferred generated-region pass exited {region.returncode}",
                "re-deriving the generated region(s) after the conflict "
                "resolution failed.",
            )

    def _rederive(
        self,
    ) -> tuple[subprocess.CompletedProcess, subprocess.CompletedProcess]:
        """The caller's pre-pass, then the `BEGIN GENERATED` region pass, output
        captured so a failure can be handed to the repair pass and echoed here.

        The second pass covers a region inside a hand-written file, which the
        caller's pre-pass does not own. prepare.sh defers one whose generator could
        not read the conflicted tree; it is resolved now."""
        runs = [
            run_pre_pass(),
            subprocess.run(
                [sys.executable, str(_SCRIPT_DIR / "regen_marked_regions.py")],
                check=False,
                capture_output=True,
                text=True,
            ),
        ]
        for done in runs:
            print(done.stdout + done.stderr, end="")
        sys.stdout.flush()
        return runs[0], runs[1]

    def _deferred_unmerged(self) -> list[str]:
        return [
            name for name in self.deferred if git_lines("ls-files", "-u", "--", name)
        ]

    def regenerate_deferred_lockfiles(self) -> None:
        """Re-derive a registry-owned lockfile whose manifest the model has now
        resolved, from that manifest — the only correct resolution of one.

        A failure here aborts: the alternative is bundling a lockfile holding
        whatever the text merge left, which is what the routing pass exists to
        prevent."""
        for name in self.deferred_lockfiles:
            try:
                touched = regenerate_lockfile(name, str(Path.cwd()))
            except LockfileError as exc:
                fail(
                    f"the deferred lockfile '{name}' could not be regenerated",
                    f"`{name}` needed re-deriving from its merged manifest after "
                    f"this resolution, and that failed: {exc}",
                )
            # touched includes the lockfile's own co-outputs (go.sum's generator
            # legitimately rewrites go.mod too), which must land in the same commit.
            git("add", "--", *touched)
            print(f"Regenerated the deferred lockfile {name} from its manifest.")

    def verify_generated_artifacts(self) -> None:
        """CONTENT post-condition for every generated artifact, not just the deferred
        ones: a cleanly text-merged generated file can hold bytes no build produces.

        This verifies and never heals, because `land`'s confinement
        replay would refuse a healed path as an edit outside the conflicted set.

        A caller that declared no pre-pass command has no generator to compare
        against, so there is no post-condition to check: its generated files, if
        any, are the ones prepare.sh already declined to defer."""
        if not PRE_PASS:
            return
        done = run_pre_pass("--verify")
        if done.returncode != 0:
            # Module-level line buffering flushes at a trailing newline; this
            # tail has none, so an explicit flush is the only thing that puts
            # it ahead of fail()'s own subprocess.run calls in the run log.
            print(done.stdout + done.stderr, end="")
            sys.stdout.flush()
            fail(
                "generated artifact(s) do not match a fresh generation",
                "one or more generated files hold bytes no build produces — they "
                "were resolved as text instead of being regenerated. Re-run the "
                "generator and push the result.",
            )

    def run_hooks(self, paths: list[str], report: Path) -> int:
        """Run the repo's hooks over `paths` and return pre-commit's own verdict
        (0 = the content passed).

        A hook that could not RUN aborts here, so it is never reported as
        a hook that rejected the content."""
        if shutil.which("pre-commit") is None:
            fail(
                "pre-commit is not installed in this job, so the merged content "
                "cannot be linted",
                "the resolution could not be linted before it was bundled "
                "(`pre-commit` is missing from the resolver job).",
                resolver_fault=True,
            )
        done = subprocess.run(
            ["pre-commit", "run", "--files", *paths],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "SKIP": ",".join(hooks_needing_the_project_env())},
        )
        body = done.stdout + done.stderr
        report.write_text(body, encoding="utf-8")
        # Same residual as verify_generated_artifacts above: no trailing newline
        # for line buffering to flush on, and the repair ladder's own subprocess
        # spawns are reachable from here without an intervening newline print.
        print(body, end="")
        sys.stdout.flush()
        if done.returncode != 0 and hook_could_not_run(body):
            fail(
                "a repo hook could not RUN in this job, so the resolved content "
                "was never verified",
                "the resolution could NOT be verified: a `pre-commit` hook could "
                "not start in the resolver job (a hook binary it needs is "
                "missing there), so nothing judged the content. That is a defect "
                "in this workflow's provisioning, **not** a problem with the "
                "resolution — see the resolver job log for the hook that failed "
                "to start.",
                resolver_fault=True,
            )
        return done.returncode

    def verify_resolved_content(self) -> None:
        """Run the repo's own hooks over exactly the paths the resolver rewrote, and
        refuse to bundle when they fail.

        Nothing downstream re-checks this content, so this refusal is the
        only thing keeping an unlinted machine-authored line off the branch."""
        # staged, not allowed: pre-commit dies on a filename it cannot open.
        if not self.staged:
            return
        if untrusted_head():
            return
        # Outside the work tree: an untracked scratch file inside it would be
        # staged by a hook or flagged by the stray-file check below.
        handle, name = tempfile.mkstemp()
        os.close(handle)
        report = Path(name)
        # The fix-then-verify contract a normal hook-run commit gets, then ONE
        # bounded model repair pass, then refuse.
        if self.run_hooks(self.staged, report) != 0:
            git("add", "--", *self.staged)
            if self.run_hooks(
                self.staged, report
            ) != 0 and not self.repair_hook_failures(report):
                fail(
                    "the resolved content fails the repo's pre-commit hooks",
                    "the resolution does not pass `pre-commit` — see the "
                    "resolver job log for the failing hook."
                    + self.marker_verdict().salvage_note(),
                )
        # A hook rewrite outside the resolved set would leave the tree disagreeing
        # with its own hooks.
        stray = git_lines("diff", "--name-only")
        if stray:
            named = " ".join(stray)
            fail(
                f"pre-commit modified file(s) outside the resolved set ('{named}')",
                "running the repo's hooks over the resolution changed files it "
                "was not asked to touch.",
            )

    def merge_carried_paths(self) -> list[str]:
        """The paths BOTH sides changed and nobody resolved: git text-merged them, so
        the bytes in the index sit in neither parent and no CI has judged them."""
        staged = set(self.staged)
        sides = [
            set(git_lines("diff", "--cached", "--name-only", "--diff-filter=d", side))
            for side in (self.checked_out_head, self.merge_base_side)
        ]
        return sorted((sides[0] & sides[1]) - staged)

    def verify_merge_carried_content(self) -> None:
        """Run the repo's own hooks over the paths the merge changed but nobody
        resolved, and FLAG the merge when they fail — never discard it.

        A clean text merge can produce a file NEITHER side contains.
        On 2026-08-12 it produced a second workflow step carrying an id another step
        already had: GitHub refuses a whole workflow file for that, so every
        auto-resolve run on that head died before it started.

        A failing hook here gets the SAME bounded model repair pass the resolved set
        gets, because the repair is the one edit that makes the merge legal. When the
        repair cannot, this lands anyway and `land` says so on the pull request. It
        does NOT refuse like :meth:`verify_resolved_content`, and the difference is
        who checks the bytes afterward: nothing re-reads the resolved set, while these
        bytes land in the pull request, where the consumer's own required pre-commit
        check judges them. So refusing buys no protection CI does not already give,
        and it costs every conflict the resolver already resolved — observed on
        agent-glovebox#4408, handed back fully conflicted with 3 of 3 resolved.
        A hook that REWRITES one of these files without failing is still refused by
        the stray check below: an auto-format nothing rejected is not a defect worth
        widening the merge for.

        Both hook passes are SKIPPED on an untrusted head — see :func:`untrusted_head`.
        """
        carried = self.merge_carried_paths()
        if not carried or untrusted_head():
            return
        handle, name = tempfile.mkstemp()
        os.close(handle)
        report = Path(name)
        if self.run_hooks(carried, report) != 0:
            # The fix-then-verify contract a normal hook-run commit gets: a hook that
            # FAILED and rewrote the file has already produced the fix.
            git("add", "--", *carried)
            if self.run_hooks(carried, report) != 0 and not self.repair_hook_failures(
                report, repairable=carried, carried=True
            ):
                self.carried_hook_failures = list(carried)
                print(
                    "::warning::the merge's own content fails this repo's pre-commit "
                    f"hooks in {' '.join(carried)}; landing the resolution and "
                    "flagging it rather than discarding every resolved conflict"
                )
                return
        stray = git_lines("diff", "--name-only")
        if stray:
            named = " ".join(stray)
            fail(
                f"pre-commit modified merge-carried file(s) ('{named}')",
                "running the repo's hooks over the merge changed files the "
                "resolution was not asked to touch.",
            )

    def commit_the_merge(self) -> None:
        """Complete the merge commit locally, with --no-verify because the index
        carries the whole base<->head delta and verify_resolved_content already
        judged the resolved set.

        The amend arm covers prepare's clean-merge path, whose merge commit exists
        only in this ephemeral checkout and was never pushed."""
        if git_status("rev-parse", "-q", "--verify", "MERGE_HEAD") == 0:
            print(git("commit", "--no-edit", "--no-verify"), end="")
        elif git_status("diff", "--cached", "--quiet") != 0:
            print(git("commit", "--amend", "--no-edit", "--no-verify"), end="")

    def run_self_review(self) -> None:
        """Read the merge commit the way the post-push watchdog will, while it is
        still local and amendable, and let a model correct what that read flags.

        Skipped only when no credential is configured; a self-review that
        RAN and refused is never skipped."""
        tokens = ordered_oauth_tokens()
        if not tokens:
            return
        before = git("rev-parse", "HEAD").strip()
        # The reviewer re-derives a rule-owned output no required check re-derives
        # (a lockfile) and annotates it away when the bytes match, rather than
        # reading a regenerated file as if a hand wrote it. Opt-in because it runs
        # the generators, and refused for a fork head for the reason PRE_PASS is:
        # a rule's command runs build backends that head's author wrote.
        verify_regenerated = "true" if PRE_PASS else "false"
        done = subprocess.run(
            ["python3", str(_SCRIPT_DIR / "self_review.py")],
            env={
                **os.environ,
                "SELF_REVIEW_TOKEN_LADDER": "\n".join(tokens),
                "AUTO_RESOLVE_VERIFY_REGENERATED": verify_regenerated,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        output = done.stdout + done.stderr
        if done.returncode != 0:
            print(output, end="" if output.endswith("\n") else "\n", file=sys.stderr)
            # Exit 2 (CANNOT-VERIFY) says nothing about the resolution, so it never
            # takes the exit-1 branch below, which judges it bad. Discarding here spends
            # the whole fan-out to punish a rate-limited credential ladder and leaves the
            # conflict for the next scan to buy again. It lands flagged instead, and
            # claude-review.yaml reads the same delta, so this pre-push read is never alone.
            if done.returncode == _SELF_REVIEW_CANNOT_VERIFY:
                self.unverified = True
                print(
                    "::warning::the merge-delta reviewer produced no verdict, so "
                    "this resolution lands UNVERIFIED: auto-merge is disabled and "
                    "a human reads it before it merges."
                )
                return
            # Exit 3 is the same verdict with a different CAUSE: the reviewer
            # flagged the resolution and no fix round fit in the wall-clock budget,
            # so no correction ran. Saying one "could not satisfy the reviewer"
            # there describes a correction that never happened.
            if done.returncode == _SELF_REVIEW_FLAGGED_UNATTEMPTED:
                fail(
                    "the resolved merge was flagged by the merge-delta reviewer, "
                    "and no fix round fit in its wall-clock budget",
                    "the resolution introduced content traceable to neither parent, "
                    "and NO automatic correction was attempted: no fix round fit in "
                    "this step's wall-clock budget. The findings, and what the "
                    "credential ladder spent, are in this run's log.",
                )
            fail(
                "the resolved merge was still flagged by the merge-delta "
                "reviewer after its fix rounds",
                "the resolution introduced content traceable to neither parent, "
                "and the automatic correction could not satisfy the reviewer. "
                "The findings are in this run's log.",
            )
        print(output, end="" if output.endswith("\n") else "\n")
        if git("rev-parse", "HEAD").strip() != before:
            self._verify_the_fixers_output(before)

    def _verify_the_fixers_output(self, before: str) -> None:
        """Re-run verify_resolved_content over the resolved set widened by whatever
        the self-review fixer touched, so its bytes are not the one content path into
        the bundle that no lint judges."""
        touched = git_lines("diff", "--name-only", before, "HEAD")
        # Minus paths the fixer deleted: pre-commit dies on a filename it cannot open.
        self.staged = [
            name
            for name in sorted(set(self.staged) | set(touched))
            if Path(name).exists()
        ]
        self.verify_resolved_content()
        # Both whole-tree post-conditions ran BEFORE the review, so a fixer amend
        # was the one content path into the bundle neither re-judged. self_review
        # restores a generated file the fixer rewrote; this is what makes that
        # restore checkable here rather than trusted.
        self.verify_generated_artifacts()
        run_post_merge_check(
            untrusted_head=untrusted_head(),
            repair=lambda report: self.repair_and_reverify(report, POST_MERGE_REJECTED),
        )
        if git_status("diff", "--cached", "--quiet") != 0:
            print(git("commit", "--amend", "--no-edit", "--no-verify"), end="")

    def write_the_bundle(self) -> None:
        """Hand the merge across the job boundary as git objects and nothing else.

        The bundle carries no claim `land` has to believe, so there is no
        metadata sidecar. Thin against both parents, which `land` already has.

        The `unverified` file beside it is not such a claim: it can only make `land`
        MORE cautious (disable auto-merge, say so on the PR), so forging it costs a
        run nothing and suppressing it lands a resolution the post-push reviewer
        still gates. Nothing `land` does on the push path reads it.
        `carried-hook-failed` is that shape too: forging it only makes `land` more
        cautious, and suppressing it lands a resolution the consumer's own required
        pre-commit check still reds. So is `rewrote-outside-conflict`, which `land`
        re-derives nothing from — it names lines for a human and turns auto-merge
        off, and the post-push merge-delta reviewer gates the merge either way.
        `rung` is the same shape: RESOLVED_RUNG_LABEL comes from the trusted workflow's own
        `||` walk over step outputs, never from repo content, and `land` re-checks
        it against the fixed `1`-`7`/`api` set before quoting it — so this file
        can carry an outright-wrong label and nothing else, whatever wrote it.
        Written unconditionally so a stale copy from an earlier step in the same
        job can never survive into the artifact."""
        self.bundle_dir.mkdir(parents=True, exist_ok=True)
        if self.unverified:
            (self.bundle_dir / "unverified").write_text(
                "the pre-push merge-delta reviewer produced no verdict\n",
                encoding="utf-8",
            )
        if self.declined:
            (self.bundle_dir / "declined").write_text(
                "".join(f"{name}\n" for name in self.declined), encoding="utf-8"
            )
        if self.carried_hook_failures:
            (self.bundle_dir / "carried-hook-failed").write_text(
                "".join(f"{name}\n" for name in self.carried_hook_failures),
                encoding="utf-8",
            )
        if self.out_of_conflict_rewrites:
            (self.bundle_dir / "rewrote-outside-conflict").write_text(
                "".join(f"{line}\n" for line in self.out_of_conflict_rewrites),
                encoding="utf-8",
            )
        (self.bundle_dir / "rung").write_text(
            os.environ.get("RESOLVED_RUNG_LABEL", "") + "\n", encoding="utf-8"
        )
        # The two parents, so a LATER run can tell whether the head it would
        # resolve is the head this bundle already resolved (reuse-bundle.py
        # reads it; `land` never does — it re-derives both from the branches).
        (self.bundle_dir / "parents.json").write_text(
            json.dumps({"head": self.checked_out_head, "base": self.merge_base_side})
            + "\n",
            encoding="utf-8",
        )
        git("update-ref", AUTO_RESOLVE_RESULT_REF, "HEAD")
        git(
            "bundle",
            "create",
            str(self.bundle_dir / "merge.bundle"),
            AUTO_RESOLVE_RESULT_REF,
            "--not",
            self.checked_out_head,
            self.merge_base_side,
        )
        head = git("rev-parse", "HEAD").strip()
        print(
            f"Bundled the resolved merge {head} (parents "
            f"{self.checked_out_head}, {self.merge_base_side}) for the land job."
        )


def main() -> None:
    for name in ("HEAD_REF", "BASE_REF", "PR", "BUNDLE_DIR"):
        if not os.environ.get(name):
            print(f"::error::{name} required", file=sys.stderr)
            raise SystemExit(1)
    # The checkout every git call below names, fixed here rather than inherited
    # per call: this step aborts a merge on its refusal path, and the working
    # directory is only known-correct at entry. _git_io's header holds why.
    bind_repo(Path.cwd())
    step = Bundle()
    step.read_parents()
    step.refuse_edits_outside_the_set()
    step.refuse_unmergeable_paths()
    step.stage_modify_delete()
    step.install_sidecar_resolutions()
    step.rederive_generated_regions()
    step.stage_text_resolutions()
    step.salvage_declined_paths()
    # Deferred paths are excluded here so a marker anywhere ELSE is diagnosed before
    # a generator handed `<<<<<<<` crashes and becomes the reported verdict.
    step.marker_verdict().refuse_leftover_markers(
        ".", *[f":(exclude){f}" for f in step.deferred]
    )
    # After the marker check, not before: a file that still carries markers looks
    # entirely rewritten against the mechanical merge, and the marker refusal
    # names the real defect more precisely than this one would.
    step.revert_out_of_conflict_rewrites()
    step.run_deferred_regeneration()
    step.verify_generated_artifacts()
    # Nothing conflicted may survive staging and regeneration.
    if git_lines("ls-files", "-u"):
        fail(
            "unmerged paths remain after staging",
            "some conflicts were not resolved.",
        )
    # The real post-condition, over the whole tree.
    step.marker_verdict().refuse_leftover_markers(".")
    step.verify_resolved_content()
    step.verify_merge_carried_content()
    run_post_merge_check(
        untrusted_head=untrusted_head(),
        repair=lambda report: step.repair_and_reverify(report, POST_MERGE_REJECTED),
    )
    step.commit_the_merge()
    step.run_self_review()
    step.write_the_bundle()


if __name__ == "__main__":
    main()
