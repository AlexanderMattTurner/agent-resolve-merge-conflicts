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
    bound_repo,
    git,
    git_lines,
    git_status,
)
from _hook_gate import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    shard_timeout_seconds,
)
from _lockfiles import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    rule_for as lockfile_rule_for,
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
# The installer ships beside the resolver's other helper scripts, so it is found
# wherever the resolver was cloned. A module constant rather than a _SCRIPT_DIR
# sibling: tests point _SCRIPT_DIR at a stub repair.py and must not reinstall.
_CLI_INSTALLER = _SCRIPT_DIR.parent / "install-claude-cli.sh"
# The installer bounds itself at ~310s (two retries of a 120s npm timeout); this
# is that plus slack, so a hung install cannot outlive the step's own budget.
_INSTALL_TIMEOUT_SECONDS = 420
# A report naming more paths than this is a whole-tree lint run, not an objection
# to a merge. The grant takes none of them rather than an arbitrary prefix.
_MAX_NAMED_PATHS = 50


def model_editable(paths: list[str]) -> list[str]:
    """PATHS minus the lockfiles, which no repair grant may carry.

    fanout.py refuses a lockfile in the file list, so one here fails every rung
    of the ladder identically and the whole pass reports "produced no usable
    run". A lockfile is re-derived by its lock command, so dropping it costs the
    repair nothing.
    """
    editable = [path for path in paths if lockfile_rule_for(path) is None]
    dropped = sorted(set(paths) - set(editable))
    if dropped:
        print(
            "repair grant drops the lockfile(s) "
            f"{' '.join(dropped)}: a lock command re-derives them, never a model."
        )
    return editable


def ensure_claude_cli() -> bool:
    """True once `claude` is on PATH, installing it at the resolver's pin when it
    is not.

    The resolve job installs the CLI inside the step that runs the model, so a run
    whose conflicts the deterministic pre-pass answered reaches this pass with no
    binary at all. Skipping the repair there is indistinguishable, from the pull
    request, from a repair pass nobody wrote.

    Run from the RESOLVER's own tree, never the merged one: `npm install -g` reads
    the working directory's `.npmrc`, so a merged tree carrying one would choose
    the registry this job installs from.
    """
    if shutil.which("claude") is not None:
        return True
    if not _CLI_INSTALLER.is_file():
        return False
    try:
        done = subprocess.run(
            ["bash", str(_CLI_INSTALLER)],
            cwd=_CLI_INSTALLER.parents[2],
            check=False,
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print("::warning::the Claude CLI installer outlived its own bound.")
        return False
    if done.returncode != 0:
        # A partial `npm install -g` leaves the bin link behind, so PATH alone
        # would report a binary whose own version check the installer failed.
        print(f"::warning::the Claude CLI installer exited {done.returncode}.")
        return False
    return shutil.which("claude") is not None


def repair_credentials(what: str) -> list[str] | None:
    """The credential ladder for a repair pass, or None once it has said why.

    WHAT names the pass in the warning, which is the only account the run gives of
    a pass that did not run.
    """
    tokens = ordered_oauth_tokens()
    if not tokens:
        print(
            f"::warning::{what}: it needs a Claude credential, and this job has none."
        )
        return None
    if not ensure_claude_cli():
        print(
            f"::warning::{what}: it needs the `claude` CLI, and this job has no CLI "
            "on PATH and could not install one."
        )
        return None
    return tokens


def _plain_file(path: str) -> bool:
    """PATH is a regular file in the working tree.

    fanout refuses a symlink entry, and an index entry whose working file the
    merge deleted, with the ORDINARY exit status — so one in the grant fails every
    rung of the credential ladder identically and reports the model as unable to
    repair it.
    """
    candidate = Path(path)
    return candidate.is_file() and not candidate.is_symlink()


def _repo_relative(candidate: str) -> str:
    """CANDIDATE as `git` spells it, or itself when it names nothing in the tree.

    A hook prints `./path.py` or an absolute path as readily as a repository-
    relative one, and the bound holds repository-relative names.
    """
    if not candidate:
        return candidate
    try:
        return str(Path(candidate).resolve().relative_to(bound_repo().resolve()))
    except (ValueError, OSError):
        return candidate


def hook_named_paths(report: Path, within: set[str]) -> list[str]:
    """The paths a hook report NAMES, out of WITHIN.

    A hook prints the file it objects to, and a merge that git text-merged into
    something a hook rejects is as often in a file no conflict named — a docstring
    citing a path the other side deleted, a config key the other side moved. The
    repair may edit what refused it, so the grant reads the refusal.

    WITHIN is the bound, and it is the bound because the REPORT IS UNTRUSTED: a
    hook runs in the merged tree and prints whatever the pull request's own
    content makes it print. Matching a token against the tracked set would grant
    the whole repository to anything that echoes a file list, so a path is granted
    only when the MERGE ITSELF changed it and no other writer owns it.
    """
    if not report.is_file() or not within:
        return []
    named = set()
    for line in report.read_text(encoding="utf-8", errors="replace").splitlines():
        for position, token in enumerate(line.split()):
            candidate, _, rest = token.strip("\"'(),[]").partition(":")
            # A SITE, never a mention: a hook prints the file it objects to at the
            # head of its line or with a `:line` suffix, while remediation advice
            # names a file mid-sentence — and granting `.pre-commit-config.yaml`
            # would let the repair satisfy the gate by editing the gate.
            if position and not rest[:1].isdigit():
                continue
            candidate = _repo_relative(candidate)
            if candidate in within and _plain_file(candidate):
                named.add(candidate)
    if len(named) > _MAX_NAMED_PATHS:
        print(
            f"::warning::the failing hook names {len(named)} of the merge's own "
            "paths, which is a whole-tree report rather than an objection: the "
            "repair grant takes none of them."
        )
        return []
    return sorted(named)


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

        The grant is narrowed HERE, at the one place that builds REPAIR_FILE_LIST,
        so a caller's `verify` set and its `git add` keep every path it watched
        fail — a lockfile the model may not write is still one the hooks re-run.
        """
        repairable = model_editable(repairable)
        if not repairable:
            print(
                "::warning::hook-repair: no file in the rejected set is one a "
                "model may edit."
            )
            return False
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

    def _hook_named_grant(self, report: Path) -> list[str]:
        """The report-named paths this pass may edit.

        The bound is the merge's own delta — every path whose merged content
        differs from at least one parent — minus every set another writer owns: a
        deferred path belongs to its generator, a modify/delete has no text, a
        declined path keeps the head's whole file, and a sidecar lives in scratch.
        """
        if not (self.checked_out_head and self.merge_base_side):
            return []
        sides = [
            set(
                git_lines(
                    "-c",
                    "core.quotePath=false",
                    "diff",
                    "--cached",
                    "--name-only",
                    "--diff-filter=d",
                    side,
                )
            )
            for side in (self.checked_out_head, self.merge_base_side)
        ]
        owned = set(
            self.deferred
            + self.deferred_lockfiles
            + self.modify_delete
            + self.declined
            + self.sidecar
        )
        return hook_named_paths(report, (sides[0] | sides[1]) - owned)

    def repair_merged_tree(self, report: Path, rejected_by: str) -> bool:
        """ONE bounded model pass over the whole merged set for a reader that is
        not the hooks — a generator, or the caller's post-merge check.

        Both read the tree as a PROGRAM, so the defect is as often in a file git
        text-merged as in one the resolver wrote: the grant covers both. True says
        a rung produced a usable run, and the CALLER re-runs its own reader to
        judge the content — this returns no verdict about it."""
        tokens = repair_credentials("no repair pass over the merged tree")
        if tokens is None:
            return False
        repairable = sorted(set(self.staged) | set(self.merge_carried_paths()))
        if not repairable:
            print(
                "::warning::no repair pass over the merged tree: no file in it is "
                "one this job may edit."
            )
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

    def repair_and_reverify(self, report: Path, rejected_by: str) -> bool:
        """Repair the merged tree, then put what the pass wrote back through every
        content gate that already ran.

        The post-merge check is the LAST gate in the step, so a repair answering
        it alone reaches the bundle judged by none of the ones before it — a
        formatting violation, or a generated file no build produces. Each gate
        refuses on its own, so a True here means the content passed them all."""
        if not self.repair_merged_tree(report, rejected_by):
            return False
        self.verify_resolved_content()
        self.verify_merge_carried_content()
        self.verify_generated_artifacts()
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
        answer, widened by every tracked path the REPORT names. ``carried`` says the
        set is one git text-merged that nobody resolved, which the prompt and the
        pass's own env state differently."""
        tokens = repair_credentials("no hook-repair pass")
        if tokens is None:
            return False
        if repairable is None:
            repairable = [name for name in self.staged if name not in set(self.sidecar)]
        verify = list(repairable if carried else self.staged)
        named = self._hook_named_grant(report)
        repairable = sorted(set(repairable) | set(named))
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
        # By what the repair CHANGED, never by what it was allowed to change: a
        # named file the pass left alone carries only its own pre-existing lint,
        # and re-verifying it would refuse a merge over a file nobody edited.
        if named:
            verify = sorted(
                set(verify) | set(git_lines("diff", "--name-only", "--", *named))
            )
        git("add", "--", *repairable)
        if self.run_hooks(verify, report) != 0:
            # The same auto-fix arm the first contract has; the rewrite must stage.
            git("add", "--", *verify)
            if self.run_hooks(verify, report) != 0:
                return False
        # main() ran this post-condition BEFORE the hooks, so a repair that edited
        # a generated file would otherwise reach the bundle judged by the hooks
        # alone and hold bytes its own generator does not produce.
        if named:
            self.verify_generated_artifacts()
        return True
