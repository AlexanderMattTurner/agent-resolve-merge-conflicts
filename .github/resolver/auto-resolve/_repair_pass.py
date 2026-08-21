"""The bounded model repair pass over an already-resolved merge tree.

PROBLEM CLASS — a merge whose CONTENT some reader rejects, where nothing about
the conflict resolution is wrong. The hooks, a generator and the caller's
post-merge check each read the merged tree, and each dies on a file git
text-merged into something that does not run. This is the one pass that fixes
that class, and `RepairPass` is what every one of those readers calls.

A mixin rather than free functions: each entry point needs the step's own
resolved set, its hook runner and its marker verdict, and threading four of
those through a call would state the coupling twice.
"""

import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _marker_verdict import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    CONFLICT_MARKER_RE,
)
from _exit_codes import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    EXIT_MISCONFIGURED,
)
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    git,
    git_status,
)
from _hook_gate import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    shard_timeout_seconds,
)
from _credentials import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    _claude_cli_env_for,
    _is_metered_credential,
    ordered_oauth_tokens,
)
from _refusal import fail  # noqa: E402,I001  # pylint: disable=wrong-import-position
from prompts import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    HOOKS_REJECTED,
)

_SCRIPT_DIR = Path(__file__).resolve().parent


class RepairPass:
    """The repair entry points, mixed into the bundle step."""

    def _walk_repair_ladder(
        self,
        report: Path,
        tokens: list[str],
        repairable: list[str],
        *,
        carried: bool = False,
        rejected_by: str = HOOKS_REJECTED,
    ) -> bool:
        """Run repair.py once per credential until one produces a usable run.

        The whole ladder shares ONE run's wall-clock budget: each rung is handed
        the time left, so a dead first credential cannot multiply the repair's
        cost by the number of rungs and push the job past its own timeout — a job
        killed there pushes nothing, which is the loss this pass exists to
        prevent.
        """
        # Under the fan-out's log dir so the repair logs ride the published
        # artifact with the shard logs; RUNNER_TEMP matches fanout.py's default.
        fanout_dir = (
            os.environ.get("FANOUT_DIR")
            or f"{os.environ.get('RUNNER_TEMP', '/tmp')}/conflict-fanout"  # noqa: S108
        )
        deadline = time.monotonic() + shard_timeout_seconds()
        for rung, token in enumerate(tokens, start=1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(
                    "::warning::hook-repair: the pass ran out of its wall-clock "
                    f"budget after {rung - 1} of {len(tokens)} credentials."
                )
                return False
            # Rounded UP: truncating would hand the first rung a budget of 0.
            left = math.ceil(remaining)
            if _is_metered_credential(token):
                print(
                    f"::warning::hook-repair: credential {rung}/{len(tokens)} is a "
                    "metered Anthropic API key, not a subscription token; this run "
                    "bills real credits."
                )
            done = subprocess.run(
                [sys.executable, str(_SCRIPT_DIR / "repair.py")],
                env={
                    **os.environ,
                    **_claude_cli_env_for(token),
                    # bundle.py owns the terminal repair verdict after every rung.
                    "PROVISIONAL_ATTEMPT": "true",
                    "SHARD_TIMEOUT_SECONDS": str(left),
                    "REPAIR_REPORT": str(report),
                    "REPAIR_FILE_LIST": "\n".join(repairable),
                    "REPAIR_DIR": f"{fanout_dir}/repair-{rung}",
                    "REPAIR_MERGE_CARRIED": "true" if carried else "",
                    "REPAIR_REJECTED_BY": rejected_by,
                },
                check=False,
            )
            if done.returncode == 0:
                return True
            # A WIRING failure is not a credential failure, so the ladder must
            # stop here. Walking past it spends every remaining rung on a wall no
            # credential can move, each failing identically while reporting
            # "produced no usable run" — which reads as the model being unable to
            # repair the file.
            if done.returncode == EXIT_MISCONFIGURED:
                print(
                    "::error::hook-repair: the pass is misconfigured — the error "
                    "above names what is missing. The remaining "
                    f"{len(tokens) - rung} credential(s) cannot fix it."
                )
                return False
            print(
                f"::warning::hook-repair: credential {rung}/{len(tokens)} "
                "produced no usable run."
            )
        return False

    def repair_merged_tree(self, report: Path, rejected_by: str) -> bool:
        """ONE bounded model pass over the whole merged set for a reader that is
        not the hooks — a generator, or the caller's post-merge check.

        Both read the tree as a PROGRAM, so the defect is as often in a file git
        text-merged as in one the resolver wrote: the grant covers both. True says
        a rung produced a usable run, and the CALLER re-runs its own reader to
        judge the content — this returns no verdict about it."""
        tokens = ordered_oauth_tokens()
        if not tokens or shutil.which("claude") is None:
            return False
        repairable = sorted(set(self.staged) | set(self.merge_carried_paths()))
        if not repairable:
            return False
        if not self._walk_repair_ladder(
            report, tokens, repairable, carried=True, rejected_by=rejected_by
        ):
            return False
        # A repair that leaves conflict markers made the tree worse than the
        # content it was fixing; refuse rather than re-verify it.
        if git_status("grep", "-nE", CONFLICT_MARKER_RE, "--", ".") == 0:
            fail(
                "the repair pass left conflict markers in the tree",
                "the automatic repair reintroduced conflict markers.",
            )
        git("add", "--", *repairable)
        return True

    def repair_hook_failures(
        self,
        report: Path,
        *,
        repairable: list[str] | None = None,
        carried: bool = False,
    ) -> bool:
        """ONE bounded model pass over the set the hooks rejected, then the
        same fix-then-verify hook contract again. True only when the repaired content
        passes; False hands the caller back to its refusal unchanged.

        The whole credential ladder shares ONE run's wall-clock budget, and the write
        grant covers ``repairable`` — the paths the caller watched fail. It defaults to
        the staged set MINUS the sidecar paths, which is the resolved-set caller's
        answer. ``carried`` says the set is one git text-merged that nobody resolved,
        which the prompt and the pass's own env state differently."""
        tokens = ordered_oauth_tokens()
        if not tokens or shutil.which("claude") is None:
            print(
                "::warning::no hook-repair pass: it needs a Claude credential "
                "and the `claude` CLI, and this job has "
                f"{'no credential' if not tokens else 'no CLI on PATH'}."
            )
            return False
        if repairable is None:
            repairable = [name for name in self.staged if name not in set(self.sidecar)]
        verify = repairable if carried else self.staged
        if not repairable:
            print(
                "::warning::no hook-repair pass: no file in the rejected set is "
                "one this job may edit."
            )
            return False
        if not self._walk_repair_ladder(report, tokens, repairable, carried=carried):
            return False
        # A repair that leaves conflict markers made the tree worse than the
        # content it was fixing; refuse rather than re-verify it.
        if git_status("grep", "-nE", CONFLICT_MARKER_RE, "--", ".") == 0:
            # Name the file and line, but not MarkerVerdict: that blames the
            # RESOLVER's denials for a marker this repair pass introduced.
            print("Conflict markers reintroduced by the hook-repair pass:")
            print(
                git("grep", "-nE", CONFLICT_MARKER_RE, "--", ".", check=False), end=""
            )
            fail(
                "the hook-repair pass left conflict markers in the tree",
                "the automatic lint repair reintroduced conflict markers.",
            )
        git("add", "--", *repairable)
        if self.run_hooks(verify, report) != 0:
            # The same auto-fix arm the first contract has; the rewrite must stage.
            git("add", "--", *verify)
            return self.run_hooks(verify, report) == 0
        return True
