"""The CALLING repository's check over the whole MERGED tree.

PROBLEM CLASS — a merge that keeps BOTH parents' definition of one name. Git
reports no conflict, and every other check the bundle step runs reads one path at
a time, so nothing notices until something reads the tree as a PROGRAM
(agent-glovebox #4340: a duplicated `_agent_home` made the module fail at import,
reddening pytest, pyright and kcov together). The caller names the command
through the workflow's `post-merge-check-command` input.

A finding the check REPORTS does not refuse the resolution. The resolution lands on
the pull request's own head, so its own checks read exactly this tree and report the
same finding, on a branch whose conflict is already resolved. Refusing throws the
resolve away and hands a human the conflict AND the finding. A check that outlives the
shared `POST_MERGE_CHECK_BUDGET_SECONDS` budget is a finding for that same reason.
What still refuses is a check that could not run, or one that wrote to the tree.

`run` RETURNS the finding rather than publishing it. The sticky pull-request comment
belongs to whichever job ends the run, and `land` rewrites it unconditionally on the
push path — the one path this reports on — so a comment written here is overwritten
minutes later and the author reads "auto-resolved" and nothing else. The caller puts
the returned text in the bundle, beside `carried-hook-failed`, and `land` renders it
into the comment it does write.
"""

import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import git  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    fail,
    report_block,
    run_or_refuse,
)


# The shell's floor for "the command never ran": 126 (found, not executable), 127
# (not found) and every 128+signal, which includes an OOM kill. Below it the
# command RAN and reported, so its status is a verdict about the merged tree.
_NEVER_RAN = 126

# ONE wall-clock budget for every invocation below — the check, the check again
# over what the repair pass wrote, and the two parent runs that attribute a
# failure. Unbounded, those four spend the resolve job's whole `timeout-minutes`
# and GitHub kills the run after the model has resolved the conflict and before
# anything is pushed (agent-glovebox #5568: two consecutive runs, 57 minutes each,
# neither pushing).
_BUDGET_ENV = "POST_MERGE_CHECK_BUDGET_SECONDS"
_DEFAULT_BUDGET_SECONDS = 600.0


def _budget_seconds() -> float:
    return float(os.environ.get(_BUDGET_ENV) or _DEFAULT_BUDGET_SECONDS)


def _left(deadline: float) -> float:
    """Seconds still owed to the caller's command, never zero.

    A non-positive remainder would read to `subprocess` as "no timeout at all",
    which is the bound this exists to enforce."""
    return max(0.001, deadline - time.monotonic())


class TreeState(NamedTuple):
    """Everything the post-merge check could change, read before and after it runs.

    Content, not status letters: a path already staged as modified keeps the same
    `M ` line when a second write changes its bytes. Not `write-tree` either — it
    refuses an unmerged index, and this must answer in every state."""

    index: str  # the mode, blob and stage of every path git tracks
    worktree: str  # the worktree's own difference from that index
    untracked: str  # every path git tracks nothing about yet


def _tree_state() -> TreeState:
    return TreeState(
        git("ls-files", "--stage"), git("diff"), git("status", "--porcelain")
    )


def _read_the_tree(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run the caller's check, echoing its report as it lands in the job log.

    Captured rather than inherited, because the repair pass needs the report as
    text: a pass handed no report has nothing to fix for.

    A command the runner cannot execute RAISES rather than reporting 126 or 127,
    so `_refuse_a_check_that_never_ran` below never sees that case;
    `run_or_refuse` names it as the plumbing fault it is. A command that overruns
    `timeout` raises too, and `run` turns that into a finding.
    """
    done = run_or_refuse(
        argv,
        label="post-merge check",
        input_name="post-merge-check-command",
        lost="check the merged tree",
        timeout=timeout,
    )
    print(done.stdout + done.stderr, end="")
    sys.stdout.flush()
    return done


# The status a shell returns for a command it could not find. Narrower than
# `_NEVER_RAN`: 126 is found-but-not-executable, which no absent file produces.
_NOT_FOUND = 127


def _absent_script(argv: list[str]) -> str:
    """The script the caller's command names that this MERGED TREE does not hold.

    A branch whose head and base both fork from before the check script landed
    carries no such file, so `bash <it>` exits 127 and reads as a missing tool. It
    is neither: that branch configured no check, and it cannot add one — the file
    it lacks is on the default branch, which is not its base.

    INVARIANT — a token counts only when it ENDS in a script extension, and this
    refusal to read a bare path is what stops the guard skipping a check that
    would have run. An ordinary check command carries path-shaped arguments that
    are not files in the worktree (`--changed-since origin/main`, `--junit-xml
    out/report.json`), and a wider rule reports `no check configured` for each."""
    return next(
        (
            token
            for token in argv
            if not token.startswith("-")
            and token.endswith((".sh", ".py", ".mjs", ".bash"))
            and not Path(token).exists()
        ),
        "",
    )


def _fails_on_its_own(argv: list[str], sha: str, timeout: float) -> bool:
    """Does this parent alone fail the same check, in a scratch worktree?

    Only a 1-to-125 status counts. A parent whose check cannot RUN there (a missing
    tool in the scratch tree) reports nothing about who owns the failure, and
    neither does one that outlives what is left of the budget."""
    with tempfile.TemporaryDirectory() as scratch:
        tree = str(Path(scratch) / "parent")
        git("worktree", "add", "--detach", tree, sha)
        try:
            done = subprocess.run(  # noqa: S603
                argv,
                cwd=tree,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            # The check's own executable is absent from this parent, because one
            # side added it. RAISING here would kill a run that had a precise
            # refusal ready to publish, so this parent simply says nothing.
            return False
        finally:
            git("worktree", "remove", "--force", tree)
    return 0 < done.returncode < _NEVER_RAN


# What must remain of the budget before a parent run STARTS. Attribution is a
# nicety — it says who owns a failure the finding already names — so a sliver of
# budget buys a run that can only be killed, and two more checkouts to do it.
_ATTRIBUTION_FLOOR_SECONDS = 30.0


def _owners_of_the_failure(
    argv: list[str], head_sha: str, base_sha: str, deadline: float
) -> list[str]:
    """The parents that fail this check on their own, so the merge is not the cause."""
    if deadline - time.monotonic() < _ATTRIBUTION_FLOOR_SECONDS:
        return []
    return [
        name
        for name, sha in (
            ("the base branch", base_sha),
            ("this pull request's head", head_sha),
        )
        if sha and _fails_on_its_own(argv, sha, _left(deadline))
    ]


def run(
    *,
    untrusted_head: bool,
    repair: Callable[[Path], bool] | None = None,
    head_sha: str = "",
    base_sha: str = "",
) -> str:
    """Run the caller's check over the merged tree, and RETURN what it finds.

    The finding rides the bundle and the merge is bundled anyway; see the module
    docstring for why it is returned rather than published. Empty when the check
    passed, ran nowhere, or is not configured. Only a check that could not RUN, or
    one that wrote to the tree, refuses — neither says anything about the merge.

    A FORK head runs none, for the reason the pre-pass runs none there: the
    command is a script that head's manifest defines, and the resolve job holds
    every model credential. An unset command is a caller that declared no check,
    and never a guess at one. ``repair`` is one bounded model pass over the merged
    tree: given the check's own report it returns whether a pass ran, and this
    re-runs the check to judge what the pass wrote."""
    if untrusted_head:
        return ""
    argv = shlex.split(os.environ.get("AUTO_RESOLVE_POST_MERGE_CHECK", ""))
    if not argv:
        return ""
    named = shlex.join(argv)
    deadline = time.monotonic() + _budget_seconds()
    # Twice at most: the check, then the check again over what one repair pass
    # wrote. A LOOP rather than a second call site, so both attempts meet the same
    # three verdict gates below — a re-run reached past them is a check whose
    # second invocation stages a file every confinement and lint check already ran.
    for attempt in range(2):
        before = _tree_state()
        try:
            done = _read_the_tree(argv, _left(deadline))
        except subprocess.TimeoutExpired:
            return _overran(named)
        _refuse_a_writing_check(named, before)
        # ASKED ONLY once the command has already failed to find something, so the
        # guard can never pre-empt a check that would have run.
        if done.returncode == _NOT_FOUND and (absent := _absent_script(argv)):
            print(
                f"::notice::the post-merge check `{named}` names `{absent}`, which "
                "the merged tree does not contain — both parents fork from before "
                "that script landed, so this branch has no check configured."
            )
            return ""
        _refuse_a_check_that_never_ran(named, done)
        if done.returncode == 0:
            return ""
        # This check is the one reader that sees the merge as a PROGRAM, so its red
        # is usually a file git text-merged into something that does not run. The
        # repair pass fixes exactly that class.
        if attempt or repair is None or not repair(_report_of(done)):
            break
    if owners := _owners_of_the_failure(argv, head_sha, base_sha, deadline):
        owned = " and ".join(owners)
        return _finding(
            f"`{named}` already fails on {owned}, so the merge is not the cause",
            f"the merged tree does not pass this repository's post-merge check "
            f"(`{named}`), and neither does {owned}, with no merge involved. Fix it "
            f"on {owned}.",
            done,
        )
    return _finding(
        "the merged tree fails the caller's post-merge check "
        f"(`{named}` exited {done.returncode})",
        f"the merged tree does not pass this repository's post-merge check "
        f"(`{named}`). A merge that keeps both sides' definition of one name raises "
        "no conflict, so this check is the only thing that reads the merge as a "
        "program, and the one repair pass could not correct what it found.",
        done,
    )


def _finding(warning: str, comment: str, done: subprocess.CompletedProcess) -> str:
    """The note `land` publishes, and the job-log warning that names it now.

    The warning is what a reader of THIS job sees; the return value is what survives
    the job boundary. `report_block` fences and truncates the check's own output, so
    a report that quotes a fence cannot end the comment early."""
    print(f"::warning::{warning}")
    sys.stdout.flush()
    report = report_block(done.stdout + done.stderr)
    return (
        "⚠️ **Auto-resolve pushed this resolution, and something in the merged tree "
        f"still needs your attention** — {comment} The conflict is resolved; this "
        "finding is not, and the pull request's own checks will report it too."
        + (f"\n\n{report}" if report else "")
        + "\n"
    )


def _overran(named: str) -> str:
    """A check that outlived its budget: a finding, never a refusal.

    Refusing would throw away a resolution the model has already been billed for,
    over a check that said nothing about the merge. The resolution lands on the
    pull request's own head, so the pull request's checks run this same command
    with the whole job to themselves and report whatever it would have found."""
    budget = _budget_seconds()
    print(
        f"::warning::the caller's post-merge check (`{named}`) did not finish "
        f"within {budget:g}s, so the merged tree went unchecked here"
    )
    sys.stdout.flush()
    return (
        "⚠️ **Auto-resolve pushed this resolution without running the post-merge "
        f"check to completion** — `{named}` did not finish within {budget:g}s "
        f"(`{_BUDGET_ENV}`), so nothing read this merge as a program. The conflict "
        "is resolved, and this pull request's own checks report what the check "
        "would have found. Raise that budget if this command needs longer.\n"
    )


def _refuse_a_writing_check(named: str, before: TreeState) -> None:
    """Every confinement, generated-artifact and lint check ran BEFORE this, so a
    file the check staged would reach the bundle judged by none of them. This is
    the only thing that keeps a read-only check read-only."""
    if _tree_state() == before:
        return
    fail(
        f"the post-merge check MODIFIED the tree it was asked to read (`{named}`)",
        f"the merged tree was not checked: `{named}` CHANGED the tree instead "
        "of reading it, and every confinement and lint check had already run. "
        "Point `post-merge-check-command` at a command that only reports — "
        "one that formats or regenerates belongs in `pre-pass-command`.",
        resolver_fault=True,
    )


def _refuse_a_check_that_never_ran(
    named: str, done: subprocess.CompletedProcess
) -> None:
    """No mark and no blame on the merge: the fix lands in this job's provisioning,
    and a re-run against the same head then answers differently. Marking it would
    strand the head until someone pushed."""
    if done.returncode < _NEVER_RAN:
        return
    fail(
        f"the caller's post-merge check could not RUN (`{named}` exited "
        f"{done.returncode}), so nothing judged the merged tree",
        f"the merged tree could NOT be checked: `{named}` exited "
        f"{done.returncode}, which means it never ran — a missing tool, or a "
        "signal that killed it. That is a defect in this workflow's "
        "provisioning, **not** a problem with the resolution or with your "
        "branch.",
        resolver_fault=True,
        report=report_block(done.stdout + done.stderr),
    )


def _report_of(done: subprocess.CompletedProcess) -> Path:
    """The check's own output, on disk, for the repair pass to fix FOR."""
    handle, name = tempfile.mkstemp()
    os.close(handle)
    report = Path(name)
    report.write_text(done.stdout + done.stderr, encoding="utf-8")
    return report
