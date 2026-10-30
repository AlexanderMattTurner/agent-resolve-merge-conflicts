"""Re-deriving the generated outputs a merge left stale.

A mixin rather than free functions: each entry point reads the step's own
deferred sets and its repair pass, and threading those through a call would
state the coupling twice. Mixed into the bundle step's `Bundle` class.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    bound_repo,
    git,
    git_lines,
    git_status,
)
from _conflict_hunks import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    CONFLICT_MARKER_RE,
)
from _lockfiles import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LockfileError,
    regenerate as regenerate_lockfile,
)

# A qualified import: PRE_PASS is a test seam patched at runtime on the module
# object, and a copied `from _pre_pass import PRE_PASS` binding here would not
# see a patch `bundle.py` reads through the same module object.
import _pre_pass  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _deletion_wins import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    parse as parse_declared,
)
from _owned import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    answer_from_env,
    load_from_env as load_owned,
)
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    command_report,
    fail,
    report_block,
)
from _tool_verdict import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    refuse_a_command_that_never_ran,
)
from prompts import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    REGEN_REJECTED,
)

_SCRIPT_DIR = Path(__file__).resolve().parent


def _left_for_a_human(paths: list[str]) -> str:
    """The sentence naming the exact paths a refusal leaves unresolved."""
    if not paths:
        return ""
    named = ", ".join(f"`{name}`" for name in paths)
    return f" Left for a human to regenerate: {named}."


def _failure_report(runs: list[subprocess.CompletedProcess | None]) -> str:
    """The first failed command among RUNS, quoted by `command_report`, with
    everything every run printed under its fold. With no failed command — a
    deferred path left at a parent's side by passes that all exited 0 — the
    combined output alone, as `report_block` quotes it."""
    done = [run for run in runs if run is not None]
    whole = "".join(run.stdout + run.stderr for run in done)
    for run in done:
        if run.returncode:
            return command_report(list(run.args), run.returncode, run.stderr, whole)
    return report_block(whole)


def _a_required_check_rederives(left: list[str]) -> bool:
    """Whether a REQUIRED check in the caller repository re-derives every
    generated file, so it goes red on each path in LEFT until a human
    regenerates it.

    INVARIANT — this is what licenses landing a merge with a generated file this
    run could not regenerate. `rederivedByCheck` asserts a required check
    compares the path against a fresh generation. A rule without it, or an
    `ownsPrefix` no check enumerates, can leave a stale file nothing flags, so
    one such rule refuses the partial landing. An unreadable answer refuses it
    too, and the caller then refuses the whole run as it would without this."""
    try:
        owned = load_owned()
        rederived = load_owned("--rederived-only")
        released = parse_declared(answer_from_env("--deletion-wins"))["release"]
    except SystemExit as exc:
        print(
            f"::warning::cannot tell which outputs a required check re-derives: {exc}"
        )
        return False
    # A release-owned path has no generator, so no regeneration can leave it stale.
    generated = {path for path in owned.exact if not released.covers(path)}
    return (
        bool(generated)
        and all(prefix in released.prefixes for prefix in owned.prefixes)
        and generated <= rederived.exact
        and set(left) <= rederived.exact
    )


def _literal(paths: list[str]) -> list[str]:
    """PATHS as pathspecs that match only themselves, never as globs."""
    return [f":(literal){path}" for path in paths]


def _index_file() -> Path:
    return bound_repo() / git("rev-parse", "--git-path", "index").strip()


def _unmerged_in(index: Path) -> set[str]:
    return {
        line.split("\t", 1)[1]
        for line in git(
            "-c", "core.quotePath=false", "ls-files", "-u", index=index
        ).splitlines()
        if line
    }


def _copy_of_the_index() -> Path:
    """A copy of the index as it stands, for `_keep_the_head_at` to restore."""
    handle, name = tempfile.mkstemp()
    os.close(handle)
    shutil.copyfile(_index_file(), name)
    return Path(name)


class DeferredRegeneration:
    """The re-derivation entry points, mixed into the bundle step."""

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

        A non-zero exit from either pass aborts, and so does a deferred path left
        unmerged that `settle_deferred_paths` could not prove current, so a
        half-derived tree is never bundled — unless `_a_required_check_rederives`
        every path left. Then the rest of the merge lands with the head's content
        at each of them, and that check flags what this run could not regenerate.
        A generator's own refusal below labels the pull request, because it fails
        the same way on every later scan. A relock is left unlabelled: it reaches
        a registry, and a retry can clear that."""
        self.regenerate_deferred_lockfiles()
        if not _pre_pass.PRE_PASS:
            # No pre-pass command, so NOTHING committed re-derives these paths.
            # A modify/delete among them is then decidable rather than stuck.
            self.deferred = self.settle_ungenerated_modify_delete(self.deferred)
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
        # The tree every gate before this one judged, so a partial landing can put
        # back whatever the failed generator and the repair pass wrote after it.
        before = _copy_of_the_index()
        rederive, region = self._rederive()
        # ASKED IMMEDIATELY, before the repair pass and before `still_unmerged`
        # below: a pre-pass that CRASHES leaves every DEFERRED_REGEN path
        # unmerged, so `still_unmerged` would otherwise fire first and blame the
        # branch for bytes that never regenerated because the tool never ran.
        refuse_a_command_that_never_ran(rederive, _pre_pass.PRE_PASS)
        refuse_a_command_that_never_ran(
            region, [sys.executable, str(_SCRIPT_DIR / "regen_marked_regions.py")]
        )
        still_unmerged, verify = self._settle(rederive, region)
        # A generator reads the merged SOURCES as a program, so it dies on a file
        # git text-merged into something that does not run — a name one side
        # renamed and the other still calls. That is the repair pass's own defect
        # class, so the tree gets one before this hands the conflict to a human.
        if rederive.returncode or region.returncode or still_unmerged:
            handle, name = tempfile.mkstemp()
            os.close(handle)
            report = Path(name)
            report.write_text(
                rederive.stdout + rederive.stderr + region.stdout + region.stderr,
                encoding="utf-8",
            )
            if self.repair_merged_tree(report, REGEN_REJECTED):
                rederive, region = self._rederive()
                refuse_a_command_that_never_ran(rederive, _pre_pass.PRE_PASS)
                refuse_a_command_that_never_ran(
                    region,
                    [sys.executable, str(_SCRIPT_DIR / "regen_marked_regions.py")],
                )
                still_unmerged, verify = self._settle(rederive, region)
        if not (rederive.returncode or region.returncode or still_unmerged):
            self.stage_regenerated_outputs()
            return
        # The failed command, its exit code and its stderr ride each refusal
        # below: the generator names the fault in a SOURCE file and its remedy,
        # where `--verify` names only a symptom in a generated one. `verify`
        # joins them because a `--verify` refusal decides `still_unmerged`.
        regen_report = _failure_report([rederive, region, verify])
        # A region sits inside a hand-written file, which no required check
        # re-derives, so a failed region pass never lands partially. Restoring
        # BEFORE re-opens every path it held unmerged, so each must be one LEFT.
        if (
            still_unmerged
            and not region.returncode
            and _unmerged_in(before) <= set(still_unmerged)
            and _a_required_check_rederives(still_unmerged)
        ):
            self._keep_the_head_at(still_unmerged, before)
            self.regen_left = still_unmerged
            self.regen_report = regen_report
            return
        if still_unmerged:
            named = " ".join(still_unmerged)
            fail(
                f"deferred generated file(s) did not regenerate cleanly ('{named}')",
                f"the generated file(s) `{named}` could not be regenerated from "
                f"the resolved sources.{_left_for_a_human(still_unmerged)}",
                report=regen_report,
                blocked=True,
            )
        # A CRASH was already refused above, before `still_unmerged`, so this is an
        # ordinary non-zero exit.
        if rederive.returncode != 0:
            fail(
                f"the deferred re-derivation pre-pass exited {rederive.returncode}",
                "re-deriving the generated file(s)/lockfile(s) after the conflict "
                "resolution failed.",
                report=regen_report,
                blocked=True,
            )
        fail(
            f"the deferred generated-region pass exited {region.returncode}",
            "re-deriving the generated region(s) after the conflict resolution failed.",
            report=regen_report,
            blocked=True,
        )

    def _keep_the_head_at(self, left: list[str], before: Path) -> None:
        """Put back the index BEFORE holds, then keep the head's content at each
        path in LEFT — the head's deletion where it deleted one — as
        `salvage_declined_paths` keeps a declined path.

        The whole index, not LEFT alone: a generator that failed part-way may have
        staged, rewritten or created other files, and none of those bytes is
        evidence of anything. The work tree then follows the index, and a file
        the generator created goes, as `refuse_edits_outside_the_set` left none."""
        shutil.copyfile(before, _index_file())
        # LEFT holds every unmerged path, which `git checkout --` refuses.
        quiet = ("-c", "core.quotePath=false")
        dirtied = sorted(set(git_lines(*quiet, "diff", "--name-only")) - set(left))
        if dirtied:
            git("checkout", "--", *_literal(dirtied))
        created = git_lines(*quiet, "ls-files", "--others", "--exclude-standard")
        if created:
            git("clean", "-q", "-f", "--", *_literal(created))
        for name in left:
            if git_status("cat-file", "-e", f"{self.checked_out_head}:{name}") == 0:
                git("checkout", self.checked_out_head, "--", *_literal([name]))
            else:
                git("rm", "-q", "-f", "--", *_literal([name]))
        self.staged = [name for name in self.staged if name not in set(left)]
        print(
            f"::warning::the generator could not regenerate {' '.join(left)}; "
            "keeping this branch's content there and landing the rest. A required "
            "check re-derives each of them and fails until a human regenerates "
            "it, and the PR names them."
        )

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
            _pre_pass.run_pre_pass(),
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

    def _settle(
        self,
        rederive: subprocess.CompletedProcess,
        region: subprocess.CompletedProcess,
    ) -> tuple[list[str], subprocess.CompletedProcess | None]:
        """`settle_deferred_paths`, then the modify/delete paths nothing here
        regenerates.

        The second pass is asked ONLY when both generators exited 0. A generator
        that crashed says nothing about whether an invocation is still committed,
        so a crash must not read as a path this repository stopped generating."""
        still_unmerged, verify = self.settle_deferred_paths(rederive, region)
        if not rederive.returncode and not region.returncode:
            still_unmerged = self.settle_ungenerated_modify_delete(still_unmerged)
        return still_unmerged, verify

    def settle_deferred_paths(
        self,
        rederive: subprocess.CompletedProcess,
        region: subprocess.CompletedProcess,
    ) -> tuple[list[str], subprocess.CompletedProcess | None]:
        """The deferred paths REDERIVE and REGION left unmerged, minus the ones
        they had already made current, which this stages; plus the `--verify`
        run when it refused, for the caller's report.

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
            return unmerged, None
        # `!= 1` because git grep answers 1 for "no match" and 2 for "could not
        # run": folding the error into the accepting branch would read a grep that
        # never looked as evidence of clean bytes.
        if git_status("grep", "-qE", CONFLICT_MARKER_RE, "--", *unmerged) != 1:
            return unmerged, None
        if any(self._is_a_parents_own_side(name) for name in unmerged):
            return unmerged, None
        done = _pre_pass.run_pre_pass("--verify")
        if done.returncode != 0:
            print(done.stdout + done.stderr, end="")
            sys.stdout.flush()
            return unmerged, done
        git("add", "--", *unmerged)
        print(
            f"Staged {len(unmerged)} deferred generated file(s) the re-derivation "
            f"left unchanged because they were already current: {' '.join(unmerged)}"
        )
        return [], None

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

    def settle_ungenerated_modify_delete(self, names: list[str]) -> list[str]:
        """NAMES minus the modify/delete paths no committed generator produces,
        each of which this resolves by honouring the deletion.

        PROBLEM CLASS — a static ownership table that outlives the generator it
        names. A path reaches the deferred set because a rule CLAIMS to generate
        it. A branch that moves a page's generation into a publish step deletes
        the page and the committed invocation in one change, and the table still
        names the directory. The regeneration rule then has NO input, and the run
        refuses a conflict that has an answer (agent-glovebox #5701: three pages
        under `docs/tla/modules/`, on two pull requests).

        Take the DELETION when all three hold:

        * this pull request's own side deleted the path, so the removal is what
          the branch is for;
        * the other side only edited the content, which is what a modify/delete
          conflict is;
        * the generators that ran here left the path's bytes at one parent's own
          side, so nothing regenerated it.

        The last is the check the static table cannot make: running the generator
        is the only thing that says whether an invocation is still committed.
        Anything short of all three stays in the list, so the caller refuses
        exactly as it did before."""
        kept: list[str] = []
        for name in names:
            decidable = self._branch_deleted_it(name) and self._is_a_parents_own_side(
                name
            )
            if not decidable:
                kept.append(name)
                continue
            git("rm", "-q", "-f", "--", name)
            print(
                f"Took the deletion of '{name}': this branch removes it, the base "
                "branch only edited it, and no generator in this tree re-derived "
                "it — so the rule that owns it has no input left."
            )
        return kept

    def _branch_deleted_it(self, name: str) -> bool:
        """Whether NAME is a modify/delete conflict THIS pull request's side
        deleted.

        The index carries a stage per side that holds the path: 1 is the merge
        base, 2 this branch, 3 the base branch. A branch that deleted the path
        leaves stages 1 and 3 and no stage 2."""
        stages = {line.split()[2] for line in git_lines("ls-files", "-u", "--", name)}
        return stages == {"1", "3"}

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
                    "this resolution, and that failed. Fix the manifest and re-run "
                    "the lock command rather than editing the lockfile."
                    f"{_left_for_a_human([name])}",
                    report=(
                        command_report(
                            exc.argv,
                            exc.returncode,
                            exc.stderr,
                            exc.stdout + exc.stderr,
                        )
                        if exc.argv
                        else report_block(str(exc))
                    ),
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
        generator to compare against, so there is no post-condition to check.

        Skipped after a partial landing: it would re-run the generator that just
        failed, and the required check `_a_required_check_rederives` found is the
        verifier of those paths now."""
        if not _pre_pass.PRE_PASS or self.regen_left:
            return
        done = _pre_pass.run_pre_pass("--verify")
        if done.returncode != 0:
            refuse_a_command_that_never_ran(done, [*_pre_pass.PRE_PASS, "--verify"])
            # Module-level line buffering flushes at a trailing newline; this
            # tail has none, so an explicit flush is the only thing that puts
            # it ahead of fail()'s own subprocess.run calls in the run log.
            print(done.stdout + done.stderr, end="")
            sys.stdout.flush()
            fail(
                "generated artifact(s) do not match a fresh generation",
                "one or more generated files hold bytes no build produces — they "
                "were resolved as text instead of being regenerated. Re-run the "
                "generator and push the result. The files `--verify` names below "
                "are left for a human.",
                report=_failure_report([done]),
            )
