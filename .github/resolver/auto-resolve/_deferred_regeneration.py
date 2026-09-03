"""Re-deriving the generated outputs a merge left stale.

A mixin rather than free functions: each entry point reads the step's own
deferred sets and its repair pass, and threading those through a call would
state the coupling twice. Mixed into the bundle step's `Bundle` class.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    git,
    git_lines,
)
from _lockfiles import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LockfileError,
    regenerate as regenerate_lockfile,
)

# A qualified import: PRE_PASS is a test seam patched at runtime on the module
# object, and a copied `from _pre_pass import PRE_PASS` binding here would not
# see a patch `bundle.py` reads through the same module object.
import _pre_pass  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
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
        half-derived tree is never bundled."""
        self.regenerate_deferred_lockfiles()
        if not _pre_pass.PRE_PASS:
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
        # ASKED IMMEDIATELY, before the repair pass and before `still_unmerged`
        # below: a pre-pass that CRASHES leaves every DEFERRED_REGEN path
        # unmerged, so `still_unmerged` would otherwise fire first and blame the
        # branch for bytes that never regenerated because the tool never ran.
        refuse_a_command_that_never_ran(rederive, _pre_pass.PRE_PASS)
        refuse_a_command_that_never_ran(
            region, [sys.executable, str(_SCRIPT_DIR / "regen_marked_regions.py")]
        )
        still_unmerged, verify_output = self.settle_deferred_paths(rederive, region)
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
                still_unmerged, verify_output = self.settle_deferred_paths(
                    rederive, region
                )
        # The generator's own output rides each refusal below: it names the
        # missing directive or the crashing source, which is the remedy a human
        # needs. `verify_output` joins it because a `--verify` that refused a
        # deferred path is what decided the first refusal below.
        regen_report = report_block(
            rederive.stdout
            + rederive.stderr
            + region.stdout
            + region.stderr
            + verify_output
        )
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
        # says a file is stale and nothing says why. A CRASH was already refused
        # above, before `still_unmerged`, so this is an ordinary non-zero exit.
        if rederive.returncode != 0:
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
        if not _pre_pass.PRE_PASS:
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
                "generator and push the result.",
                report=report_block(done.stdout + done.stderr),
            )
