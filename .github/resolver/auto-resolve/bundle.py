#!/usr/bin/env python3
"""Auto-resolve merge conflicts — BUNDLE step (the untrusted half of finalize).

Verifies the working tree is fully resolved (no unmerged paths, no stray conflict
markers, no edit outside the conflicted set), completes the merge commit LOCALLY,
and writes it to $BUNDLE_DIR as a git bundle for the separate `land` job.

Everything above this step in the `resolve` job is PR-authored or model-authored,
and this script runs in the same job, so it pushes nothing and holds no push
credential: its commit is UNTRUSTED OUTPUT that auto-resolve/land.sh re-derives
every property of from git, and this step fails LOUD rather than bundle a
half-resolved tree. A wrong auto-resolution must never reach the branch.

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
  AUTO_RESOLVE_SETUP_RECORD            what the caller's setup-command changed
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
from _slow_run import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    write_sidecar,
)
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    escalation_block,
    fail,
    report_block,
    run_or_refuse,
)
from _tool_verdict import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    refuse_a_command_that_never_ran,
)
from _setup_record import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    undo_setup_changes,
)
from _unmergeable import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    refuse_unmergeable,
)
from _widened import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    revert_whitespace_only_edits,
    settle_widened_edits,
)
from prompts import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    POST_MERGE_REJECTED,
    REGEN_REJECTED,
)
from _self_review_gate import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    review_and_verify,
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


def untrusted_head() -> bool:
    """Whether this run merged a head the resolve job may not execute — a fork.

    INVARIANT — the hook passes below run the MERGED tree's own pre-commit hooks,
    which on a fork head are code the fork's author wrote. This refusal is what
    keeps a fork's code out of the job holding every model credential; the pull
    request's own required checks judge the merged bytes instead."""
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


def git_add_if_any(paths: list[str]) -> None:
    """`git add` PATHS, and nothing at all for an empty list — a bare `git add --`
    with no pathspec would stage the whole tree."""
    if paths:
        git("add", "--", *paths)


def env_list(name: str) -> list[str]:
    """A whitespace-separated path list, the way bash's `read -ra` splits one."""
    return os.environ.get(name, "").split()


class Bundle(RepairPass):
    """One run of the step: what the resolver was asked to resolve, what it left
    in the tree, and the state the checks below accumulate."""

    def __init__(self) -> None:
        self.pr = os.environ["PR"]
        self.bundle_dir = Path(os.environ["BUNDLE_DIR"])
        self.allowed = env_list("CONFLICT_LIST")
        # The unconflicted files this PR changed, which a shard may Edit when
        # its resolution reaches into one; `widened` is the subset it did edit.
        self.writable = env_list("WRITABLE_LIST")
        self.widened: list[str] = []
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
        self.reviewed = False
        self.declined: list[str] = []
        self.carried_hook_failures: list[str] = []
        self.out_of_conflict_rewrites: list[str] = []
        self.post_merge_finding = ""
        # ONE bounded model pass per RUN, not per call site. The post-merge check
        # runs a second time when the self-review fixer amends HEAD, and each pass
        # costs a full repair ladder plus two more check invocations — on exactly
        # the runs that already failed the check.
        self.repair_pass_spent = False

    def repair_post_merge_once(self, report: Path) -> bool:
        """The run's single repair pass, whichever post-merge call reaches it first."""
        if self.repair_pass_spent:
            return False
        self.repair_pass_spent = True
        return self.repair_and_reverify(report, POST_MERGE_REJECTED)

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
        resolve and the files this PR itself changed; any other modified tracked
        file, or any new untracked file, aborts the run. Checked BEFORE staging,
        and this is where the edits in the second set are recorded, so they are
        staged with the resolutions and named on the pull request."""
        unmerged = {line.split("\t")[-1] for line in git_lines("ls-files", "-u")}
        allowed = set(self.allowed)
        writable = set(self.writable) - allowed
        for name in git_lines("diff", "--name-only"):
            if name in unmerged or name in allowed:
                continue
            if name in writable:
                self.widened.append(name)
                continue
            fail(
                f"the resolver modified a file outside the conflicted set ('{name}')",
                "the LLM edited a file it was not asked to touch and this pull "
                "request never changed. A `setup-command` change is undone before "
                "this check, so this is not one of those.",
            )
        if git_lines("ls-files", "--others", "--exclude-standard"):
            fail(
                "the resolver created new untracked files",
                "the LLM added files it was not asked to.",
            )

    def refuse_unmergeable_paths(self) -> None:
        """no unmergeable path (a `-merge`-attributed lockfile, a binary)
        may sit in CONFLICT_LIST; an edit-based resolution of one is unverifiable."""
        refuse_unmergeable(self.allowed, f"origin/{os.environ['BASE_REF']}")

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
        that walks the whole tree can die on one of them. Here the siblings carry
        the model's resolutions, so the generator reads a tree that parses and the
        region resolves with no model at all.

        Ordered before `stage_text_resolutions`, which stages every conflicted path
        whether or not it is resolved: after it nothing is unmerged, and this pass
        reads the unmerged set."""
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
        resolved = [n for n in self.allowed if n not in decided]
        if resolved:
            git("add", "--", *resolved)
        # The widened edits join `staged` now, so every later pass reads them, but
        # they reach the index in stage_widened_edits, after the declines are known.
        self.staged = resolved + self.widened

    def stage_widened_edits(self) -> None:
        """Stage the edits the model made in files this PR changed, minus the
        ones only a shard that then DECLINED made, and minus the ones that only
        re-space the merge. After salvage_declined_paths, which is what names
        the declines."""
        # The revert runs FIRST: both put a path back from the INDEX, and
        # settle_widened_edits stages what it keeps, which would make the
        # model's own bytes the content a later checkout restores.
        kept = settle_widened_edits(revert_whitespace_only_edits(self.widened))
        dropped = set(self.widened) - set(kept)
        self.widened = kept
        self.staged = [n for n in self.staged if n not in dropped]

    def salvage_declined_paths(self) -> None:
        """Keep the head's content at a path the model DECLINED, so one declined file
        does not discard every other file this run resolved — a whole-tree marker
        check over per-path work would otherwise throw away every resolved file for
        the sake of the one that kept its markers.

        Only a DELIBERATE decline is salvaged, and the shard's own decline RECORD is
        what says a path is one. Every other cause returns untouched for
        :class:`MarkerVerdict` to refuse: a permission denial means the write path
        was closed, and a shard that reported success while answering nothing is a
        harness fault with no judgement behind it — keeping the head's content for
        either would silently drop an edit nobody chose to drop. Salvaging nothing
        is also a refusal — a run whose every conflicted path declined resolved
        nothing to land."""
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
        the change and lands it rather than costing the PR a handoff over hunks that
        were sound: `land` names the lines and turns auto-merge off, so the
        merge-delta reviewer reads them before anyone merges.

        `refuse_edits_outside_the_set` is the same question one level up, over whole
        paths, and cannot see this one: a conflicted file is in the set, so a
        rewrite of its untouched context reads as part of the resolution.

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
                # The bundled file now matches the mechanical merge outside every
                # span, so this path reports nothing and auto-merge stays armed.
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
        history has several bases. False when the path is absent from a base, and
        false when the base side matches the head too."""
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
        """Re-derive the generated outputs the merge made stale — a whole
        rule-owned file, and a `BEGIN GENERATED` region inside a hand-written one.

        The DEFERRED set is not the bound: an input only one side changed
        conflicts nowhere, so git merges both sides and the tree keeps bytes no
        build produces, which `verify_generated_artifacts` below would then refuse
        as a wrong resolution. So the caller's pre-pass runs over the staged merge
        whatever conflicted, re-deriving from the sources as they now stand — this
        also covers a generated file that text-merged cleanly itself and so
        appears in no deferred list while holding stale bytes.

        A still-unmerged deferred path and a non-zero exit from either pass both
        abort, so a half-derived tree is never bundled."""
        self.regenerate_deferred_lockfiles()
        if not PRE_PASS:
            if not self.deferred:
                # No generator to run, so nothing here is stale that this could
                # see: a caller with derived files declares the command.
                return
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
        # The generator's own output rides each refusal below: it names the
        # missing directive or the crashing source, which is the remedy a human
        # needs, while the downstream `--verify` line names only a stale byte.
        regen_report = report_block(
            rederive.stdout + rederive.stderr + region.stdout + region.stderr
        )
        still_unmerged = self._deferred_unmerged()
        if still_unmerged:
            named = " ".join(still_unmerged)
            fail(
                f"deferred generated file(s) did not regenerate cleanly ('{named}')",
                f"the generated file(s) `{named}` could not be regenerated from "
                "the resolved sources.",
                report=regen_report,
            )
        # The generator's own output is the report, because it names the fault in a
        # SOURCE file and the remedy for it, while the `--verify` refusal below
        # names a symptom in a generated one. Without it the pull request's comment
        # says a file is stale and nothing says why.
        if rederive.returncode != 0:
            refuse_a_command_that_never_ran(rederive, PRE_PASS)
            fail(
                f"the deferred re-derivation pre-pass exited {rederive.returncode}",
                "re-deriving the generated file(s)/lockfile(s) after the conflict "
                "resolution failed.",
                report=regen_report,
            )
        if region.returncode != 0:
            fail(
                f"the deferred generated-region pass exited {region.returncode}",
                "re-deriving the generated region(s) after the conflict "
                "resolution failed.",
                report=regen_report,
            )
        self.stage_regenerated_outputs()

    def stage_regenerated_outputs(self) -> None:
        """Stage whatever the re-derivation left in the work tree.

        A caller's pre-pass stages the outputs it rewrites, and nothing here can
        require that of it. An unstaged re-derivation reaches neither the commit
        nor the branch, and `verify_resolved_content`'s stray-file check then
        reports it as pre-commit rewriting a file nobody resolved.

        INVARIANT — every path this stages was written by the CALLER'S OWN
        generators: `refuse_edits_outside_the_set` left nothing modified outside
        the conflicted set, and every writer between it and here (the pre-pass,
        the region pass, the model repair pass) stages what it writes or aborts
        the run. A fork head runs none.

        `core.quotePath=false` because a C-quoted path is one `git add` then
        matches nothing, which would abort every resolution in a repository
        holding a non-ASCII generated name."""
        quiet = ("-c", "core.quotePath=false")
        # `u` DROPS the unmerged paths: one is dirty because nothing resolved it,
        # so staging it would settle a conflict by taking the work tree's side —
        # silently, because the check that refuses an unmerged path outside the
        # resolved set reads the index this leaves.
        dirty = git_lines(*quiet, "diff", "--name-only", "--diff-filter=u")
        # An output the generator CREATED is untracked, so the modified list
        # never names it and the commit would ship without the file `--verify`
        # just passed on.
        created = git_lines(*quiet, "ls-files", "--others", "--exclude-standard")
        outputs = [*dirty, *created]
        if not outputs:
            return
        git("add", "--", *outputs)
        print(
            f"Staged {len(outputs)} re-derived generated file(s) the pre-pass "
            f"left unstaged: {' '.join(outputs)}"
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
        if not self.deferred_lockfiles:
            return
        # The common ancestor of the two parents, so a conflicted lockfile is
        # reseeded from it rather than deleted — see _lockfiles.regenerate's
        # `seed_ref`. Without it the relock re-resolves every transitive
        # dependency from nothing, drifting past what the merge actually forced.
        seed_ref = git(
            "merge-base", self.checked_out_head, self.merge_base_side
        ).strip()
        for name in self.deferred_lockfiles:
            try:
                touched = regenerate_lockfile(name, str(Path.cwd()), seed_ref)
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

        This verifies and never heals: `run_deferred_regeneration` is the healing
        pass and already ran, so bytes still stale here mean the generator itself
        refuses to produce them. A caller that declared no pre-pass command has no
        generator to compare against, so there is no post-condition to check."""
        if not PRE_PASS:
            return
        done = run_pre_pass("--verify")
        if done.returncode != 0:
            refuse_a_command_that_never_ran(done, PRE_PASS)
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
                report=report_block(done.stdout + done.stderr),
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
                "resolution.",
                resolver_fault=True,
                report=report_block(body),
            )
        return done.returncode

    def hook_written_lockfiles(self) -> list[str]:
        """The lockfiles the hook run itself just rewrote in the work tree.

        A repo's regen hook re-derives its lockfile from the merged manifest, so
        those bytes are its own lock command's, produced inside this job — never a
        model's. Leaving them unstaged refuses the merge over a file the repo's own
        hook would rewrite identically on the next run.

        INVARIANT — a path here is one a lockfile RULE owns and one a hook run just
        rewrote, so it cannot be a model's: every model write happened before
        `refuse_edits_outside_the_set`, and each is staged. `model_editable` keeps
        it that way by dropping every lockfile from the repair grant."""
        written = [
            name
            for name in git_lines("-c", "core.quotePath=false", "diff", "--name-only")
            if lockfile_rule_for(name)
        ]
        if written:
            print(
                "Staging the lockfile(s) the repo's own hooks re-derived while "
                f"they ran: {' '.join(written)}"
            )
        return written

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
            recheck = self.staged + self.hook_written_lockfiles()
            git("add", "--", *recheck)
            if self.run_hooks(recheck, report) != 0 and not self.repair_hook_failures(
                report
            ):
                fail(
                    "the resolved content fails the repo's pre-commit hooks",
                    "the resolution does not pass `pre-commit`."
                    + self.marker_verdict().salvage_note(),
                    report=report_block(report.read_text(encoding="utf-8")),
                )
        # A hook rewrite outside the resolved set would leave the tree disagreeing
        # with its own hooks. A lockfile is the exception the arm above already
        # makes, and a regen hook that rewrites one WITHOUT failing reaches only
        # here: same hook, same bytes, so it takes the same answer.
        git_add_if_any(self.hook_written_lockfiles())
        stray = git_lines("diff", "--name-only")
        if stray:
            named = " ".join(stray)
            fail(
                f"pre-commit modified file(s) outside the resolved set ('{named}')",
                "running the repo's hooks over the resolution changed files it "
                f"was not asked to touch: `{named}`.",
                report=report_block(report.read_text(encoding="utf-8")),
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

        A clean text merge can produce a file NEITHER side contains — two workflow
        steps landing the same id is one shape of that, and GitHub refuses the whole
        workflow file for it.

        A failing hook here gets the SAME bounded model repair pass the resolved set
        gets, because the repair is the one edit that makes the merge legal. When the
        repair cannot, this lands anyway and `land` says so on the pull request,
        rather than refusing like :meth:`verify_resolved_content`: these bytes land
        where the consumer's own required pre-commit check judges them, so refusing
        buys no protection CI does not already give, and it costs every conflict the
        resolver already resolved. A hook that REWRITES one of these files without
        failing is still refused by the stray check below.

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
            recheck = carried + self.hook_written_lockfiles()
            git("add", "--", *recheck)
            if self.run_hooks(recheck, report) != 0 and not self.repair_hook_failures(
                report, repairable=carried, carried=True
            ):
                self.carried_hook_failures = list(carried)
                print(
                    "::warning::the merge's own content fails this repo's pre-commit "
                    f"hooks in {' '.join(carried)}; landing the resolution and "
                    "flagging it rather than discarding every resolved conflict"
                )
                return
        git_add_if_any(self.hook_written_lockfiles())
        stray = git_lines("diff", "--name-only")
        if stray:
            named = " ".join(stray)
            fail(
                f"pre-commit modified merge-carried file(s) ('{named}')",
                "running the repo's hooks over the merge changed files the "
                f"resolution was not asked to touch: `{named}`.",
                report=report_block(report.read_text(encoding="utf-8")),
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

        Skipped only when the caller passed `self-review: false` or configured
        no credential; a self-review that RAN and refused is never skipped."""
        if os.environ.get("AUTO_RESOLVE_SELF_REVIEW") != "true":
            print(
                "::notice::self-review off: the caller turned it off, so the "
                "review of record for this resolution is whatever reads it "
                "after the push."
            )
            return
        tokens = ordered_oauth_tokens()
        if not tokens:
            # `unverified` is the true claim here, and it is what stops
            # reuse-bundle.py refusing this bundle forever: no run without a
            # credential can write `self-reviewed`, so every later run for this
            # head would rebuild the same unmarked bundle. Silence would read as
            # a review that ran and found nothing.
            print(
                "::warning::self-review skipped: the review is on, and no "
                "credential rung is configured, so nothing read this merge "
                "before the push."
            )
            self.unverified = True
            return
        # The reviewer re-derives a rule-owned output no required check re-derives
        # (a lockfile) and annotates it away when the bytes match, rather than
        # reading a regenerated file as if a hand wrote it. Opt-in because it runs
        # the generators, and refused for a fork head for the reason PRE_PASS is:
        # a rule's command runs build backends that head's author wrote.
        verify_regenerated = "true" if PRE_PASS else "false"
        # Re-proved HERE, never carried from verify_generated_artifacts: the
        # hook and repair passes between that call and this commit may rewrite
        # a generated file, and the flag claims the tree the renderer READS.
        # A `--verify` that no longer passes drops the claim, so the renderer
        # falls back to its own scratch re-derivation (fail-toward-review).
        pre_pass_verified = (
            "true" if PRE_PASS and run_pre_pass("--verify").returncode == 0 else "false"
        )
        if verify_regenerated == "true" and pre_pass_verified != "true":
            print(
                "::warning::a hook or repair pass changed a generated file after "
                "the pre-pass verification; the merge-delta renderer will "
                "re-derive the generated outputs itself."
            )
        review_and_verify(
            self,
            tokens=tokens,
            verify_regenerated=verify_regenerated,
            pre_pass_verified=pre_pass_verified,
            untrusted=untrusted_head(),
        )

    def write_the_bundle(self) -> None:
        """Hand the merge across the job boundary as git objects and nothing else.

        The bundle carries no claim `land` has to believe, so there is no
        metadata sidecar. Thin against both parents, which `land` already has.

        The `unverified` file beside it is not such a claim: it can only make `land`
        MORE cautious, so forging it costs a run nothing and suppressing it lands a
        resolution the post-push reviewer still gates.
        `carried-hook-failed` and `post-merge-check-failed` are that shape too.
        `widened` can only NARROW what `land` re-derives: a path it names that `land`
        does not derive as writable is ignored, and one it omits is reported as an
        out-of-conflict write. `rewrote-outside-conflict` is the one sidecar `land`
        cannot re-derive, so it must not fail open: `land` checks both fields against
        the shapes written here before quoting them into a privileged comment,
        reports an unparsable record rather than skipping it, and only ever turns
        auto-merge off on what it reads.
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
        # reuse-bundle.py refuses a bundle without this marker for a caller that
        # runs the self-review, so a resolution produced while the review was
        # off can never be reused past a caller that has it on.
        if self.reviewed:
            (self.bundle_dir / "self-reviewed").write_text(
                "the pre-push merge-delta reviewer read this resolution\n",
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
        if self.post_merge_finding:
            (self.bundle_dir / "post-merge-check-failed").write_text(
                self.post_merge_finding, encoding="utf-8"
            )
        if self.widened:
            (self.bundle_dir / "widened").write_text(
                "".join(f"{name}\n" for name in self.widened), encoding="utf-8"
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
    # Written before any stage below can hang: self-review and the fan-out are the
    # long stages, so a run killed mid-way still leaves `land` the sizes it needs
    # for the slow-run advisory, even though this step's own outcome never lands.
    write_sidecar(step.bundle_dir, len(step.allowed))
    step.read_parents()
    # BEFORE the edits-outside-the-set check, which cannot tell a tree repair the
    # caller asked for from a file the model touched. This puts every path the
    # setup command changed back, so the check judges the resolution alone.
    undo_setup_changes()
    step.refuse_edits_outside_the_set()
    step.refuse_unmergeable_paths()
    step.stage_modify_delete()
    step.install_sidecar_resolutions()
    step.rederive_generated_regions()
    step.stage_text_resolutions()
    step.salvage_declined_paths()
    step.stage_widened_edits()
    # Both lists are excluded so a marker anywhere ELSE is diagnosed before a
    # generator handed `<<<<<<<` crashes and becomes the reported verdict. The
    # lockfiles need it too: a conflicted one still carries its markers here by
    # design, so this gate aborted the run before `run_deferred_regeneration`
    # below could re-derive it. The whole-tree check after that call holds them.
    step.marker_verdict().refuse_leftover_markers(
        ".",
        *[f":(exclude){f}" for f in (*step.deferred, *step.deferred_lockfiles)],
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
    step.post_merge_finding = run_post_merge_check(
        untrusted_head=untrusted_head(),
        repair=step.repair_post_merge_once,
        head_sha=step.checked_out_head,
        base_sha=step.merge_base_side,
    )
    step.commit_the_merge()
    step.run_self_review()
    step.write_the_bundle()


if __name__ == "__main__":
    main()
