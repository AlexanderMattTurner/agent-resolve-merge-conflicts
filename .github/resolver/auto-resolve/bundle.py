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
    rule_for as lockfile_rule_for,
)
from _marker_verdict import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    CONFLICT_MARKER_RE,
    MarkerVerdict,
    declined_files,
    files_with_no_deliverable,
    marker_file_text,
)
from _neither_side import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    NeitherSideReport,
)
from _out_of_conflict import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    OutOfConflictRevert,
)
from _post_merge_check import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    new_budget as new_post_merge_budget,
    run as run_post_merge_check,
)

# A qualified import, not `from _pre_pass import PRE_PASS`: PRE_PASS is a test
# seam patched at RUNTIME, and a copied binding here would not see a patch
# `_deferred_regeneration` reads through the same module object.
import _pre_pass  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _deferred_regeneration import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    DeferredRegeneration,
)
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


def git_add_if_any(paths: list[str]) -> None:
    """`git add` PATHS, and nothing at all for an empty list — a bare `git add --`
    with no pathspec would stage the whole tree."""
    if paths:
        git("add", "--", *paths)


def env_list(name: str) -> list[str]:
    """A whitespace-separated path list, the way bash's `read -ra` splits one."""
    return os.environ.get(name, "").split()


class Bundle(RepairPass, DeferredRegeneration, OutOfConflictRevert, NeitherSideReport):
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
        # What stage_modify_delete decided, kept so a later pass can check the
        # decision still holds. Empty until that pass runs.
        self.modify_delete_decisions: dict[str, str] = {}
        self.sidecar = env_list("SIDECAR_PATHS")
        self.deferred = env_list("DEFERRED_REGEN")
        # Lockfiles the resolver's own registry owns, deferred because their
        # manifest was conflicted when prepare ran. A fork head regenerates none
        # of them, for the reason PRE_PASS is empty there.
        self.deferred_lockfiles = (
            [] if _pre_pass.untrusted_head() else env_list("DEFERRED_LOCKFILES")
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
        self.neither_side_lines: list[str] = []
        self.post_merge_finding = ""
        # ONE bounded model pass per RUN, not per call site. The post-merge check
        # runs a second time when the self-review fixer amends HEAD, and each pass
        # costs a full repair ladder plus two more check invocations — on exactly
        # the runs that already failed the check.
        self.repair_pass_spent = False
        # ONE wall-clock budget per RUN, for the same reason. Stamped on first use
        # rather than here, so the merge that runs before the check keeps none of it.
        self._post_merge_deadline: float | None = None

    def repair_post_merge_once(self, report: Path) -> bool:
        """The run's single repair pass, whichever post-merge call reaches it first."""
        if self.repair_pass_spent:
            return False
        self.repair_pass_spent = True
        return self.repair_and_reverify(report, POST_MERGE_REJECTED)

    def post_merge_deadline(self) -> float:
        """The run's single post-merge budget, whichever call reaches it first."""
        if self._post_merge_deadline is None:
            self._post_merge_deadline = new_post_merge_budget()
        return self._post_merge_deadline

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
                self.modify_delete_decisions[name] = decision
            elif decision == "delete":
                git("rm", "-q", "-f", "--", name)
                self.modify_delete_decisions[name] = decision
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

    def refuse_a_verdict_regeneration_undid(self) -> None:
        """Refuse when re-derivation changed whether a decided path exists.

        A rule can own a modify/delete path, and re-derivation then writes the
        file back after a `delete` verdict or removes it after a `keep` one. The
        commit would carry the opposite of what the resolver decided, with the
        verdict record still saying otherwise.
        """
        for name, decision in self.modify_delete_decisions.items():
            staged = bool(git_lines("ls-files", "--", name))
            if staged == (decision == "keep"):
                continue
            became = "back" if staged else "gone"
            fail(
                f"re-derivation put '{name}' {became} after a '{decision}' verdict",
                f"`{name}` is a modify/delete conflict the resolver decided to "
                f"{decision}, and a generator that owns it then put it {became}. "
                "The commit would carry the opposite of the recorded verdict. "
                "That is a defect in this workflow's plumbing, **not** a hard "
                "conflict.",
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

    def settle_deferred_paths(
        self,
        rederive: subprocess.CompletedProcess,
        region: subprocess.CompletedProcess,
    ) -> tuple[list[str], str]:
        """The deferred paths REDERIVE and REGION left unmerged, minus the ones
        they had already made current, which this stages; plus whatever a
        `--verify` that refused printed, for the caller's report.

        PROBLEM CLASS — an idempotent producer that reports "nothing to do" reads
        as a producer that failed. prepare.sh's own pre-pass re-derives these
        paths before the model runs, so when it wrote the merged tree's output
        the pass here finds the bytes current, writes nothing and stages nothing.
        The index then still holds the conflict stages, and reading the index
        calls that a generator that could not produce the file.

        So ask for EVIDENCE that a generator wrote these bytes, and take four
        answers together, because none of them alone is that evidence:

        - both passes exited 0, so no generator refused this tree;
        - the marker scan finds no conflict text, which no generator writes;
        - every path's bytes differ from all of its own unmerged stages
          (`_is_a_parents_own_side`), so no path is git's own untouched side;
        - `--verify` says the work tree matches a fresh generation.

        All four hold, so the work tree IS the re-derivation and staging it
        resolves the path. Any one fails and the whole list stands, so the caller
        refuses exactly as it did before.

        Its own `--verify` run, never one shared with `verify_generated_artifacts`
        below: that gate runs again after the post-merge repair pass rewrites the
        tree, and an answer carried across a writer proves nothing about what it
        wrote."""
        unmerged = self._deferred_unmerged()
        if not unmerged or rederive.returncode or region.returncode:
            return unmerged, ""
        # `!= 1` because git grep answers 1 for "no match" and 2 for "could not
        # run": folding the error into the accepting branch would read a grep that
        # never looked as evidence of clean bytes.
        if git_status("grep", "-qE", CONFLICT_MARKER_RE, "--", *unmerged) != 1:
            return unmerged, ""
        if any(self._is_a_parents_own_side(name) for name in unmerged):
            return unmerged, ""
        done = _pre_pass.run_pre_pass("--verify")
        if done.returncode != 0:
            print(done.stdout + done.stderr, end="")
            sys.stdout.flush()
            return unmerged, done.stdout + done.stderr
        git("add", "--", *unmerged)
        print(
            f"Staged {len(unmerged)} deferred generated file(s) the re-derivation "
            f"left unchanged because they were already current: {' '.join(unmerged)}"
        )
        return [], ""

    def _is_a_parents_own_side(self, name: str) -> bool:
        """Whether NAME's work-tree bytes are one of its own unmerged stages, or
        NAME has no work-tree file at all.

        `--verify` is a WHOLE-TREE answer, so it says nothing about a path the
        caller's generators do not own — and prepare.sh routes a generated-owned
        path into the deferred set ahead of the mergeability test, so the set can
        hold a binary or a `-merge` file. git leaves
        each of those at one parent's side with no markers, which every other gate
        here reads as clean. Staging that commits "ours" as the resolution, the
        same refusal `stage_text_resolutions` names.

        So only bytes NO parent wrote are evidence a generator produced them. A
        generator whose output happens to equal one side is refused too: this
        fails closed, on the state the run had before this method existed."""
        blob = git("hash-object", "--", name, check=False).strip()
        stages = {line.split()[1] for line in git_lines("ls-files", "-s", "--", name)}
        return not blob or blob in stages

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
        if _pre_pass.untrusted_head():
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
        if not carried or _pre_pass.untrusted_head():
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
        verify_regenerated = "true" if _pre_pass.PRE_PASS else "false"
        # Re-proved HERE, never carried from verify_generated_artifacts: the
        # hook and repair passes between that call and this commit may rewrite
        # a generated file, and the flag claims the tree the renderer READS.
        # A `--verify` that no longer passes drops the claim, so the renderer
        # falls back to its own scratch re-derivation (fail-toward-review).
        pre_pass_verified = (
            "true"
            if _pre_pass.PRE_PASS and _pre_pass.run_pre_pass("--verify").returncode == 0
            else "false"
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
            untrusted=_pre_pass.untrusted_head(),
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
        out-of-conflict write. `rewrote-outside-conflict` and `wrote-neither-side`
        are the sidecars `land` cannot re-derive, so neither may fail open: `land`
        checks both fields of each against the shapes written here before quoting
        them into a privileged comment, reports an unparsable record rather than
        skipping it, and only ever turns auto-merge off on what it reads.
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
        if self.neither_side_lines:
            (self.bundle_dir / "wrote-neither-side").write_text(
                "".join(f"{line}\n" for line in self.neither_side_lines),
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
    step.refuse_a_verdict_regeneration_undid()
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
        untrusted_head=_pre_pass.untrusted_head(),
        repair=step.repair_post_merge_once,
        head_sha=step.checked_out_head,
        base_sha=step.merge_base_side,
        deadline=step.post_merge_deadline(),
    )
    # AFTER the post-merge check, not before: its repair rewrites the merged tree
    # and re-runs the hooks, so a report taken earlier names lines that have moved
    # and misses the ones the repair itself wrote. LAST of the content passes, so
    # these numbers index the tree the commit below takes.
    step.report_lines_from_neither_side()
    step.commit_the_merge()
    step.run_self_review()
    step.write_the_bundle()


if __name__ == "__main__":
    main()
