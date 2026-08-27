"""The CALLING repository's check over the whole MERGED tree.

PROBLEM CLASS — a merge that keeps BOTH parents' definition of one name. Git
reports no conflict, and every other check the bundle step runs reads one path at
a time, so nothing notices until something reads the tree as a PROGRAM
(agent-glovebox #4340: a duplicated `_agent_home` made the module fail at import,
reddening pytest, pyright and kcov together). The caller names the command
through the workflow's `post-merge-check-command` input, and a non-zero exit
refuses the resolution rather than pushing it.
"""

import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import git  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PARENT_ALREADY_FAILS,
    fail,
    report_block,
    run_or_refuse,
)


# The shell's floor for "the command never ran": 126 (found, not executable), 127
# (not found) and every 128+signal, which includes an OOM kill. Below it the
# command RAN and reported, so its status is a verdict about the merged tree.
_NEVER_RAN = 126


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


def _read_the_tree(argv: list[str]) -> subprocess.CompletedProcess:
    """Run the caller's check, echoing its report as it lands in the job log.

    Captured rather than inherited, because the repair pass needs the report as
    text: a pass handed no report has nothing to fix for.

    A command the runner cannot execute RAISES rather than reporting 126 or 127,
    so `_refuse_a_check_that_never_ran` below never sees that case;
    `run_or_refuse` names it as the plumbing fault it is.
    """
    done = run_or_refuse(
        argv,
        label="post-merge check",
        input_name="post-merge-check-command",
        lost="check the merged tree",
    )
    print(done.stdout + done.stderr, end="")
    sys.stdout.flush()
    return done


# A token the caller's command names as the script to run, rather than a flag or the
# interpreter's own name. `bash x.sh` puts the script in argv[1]; `./check.sh` in argv[0].
def _named_scripts(argv: list[str]) -> list[str]:
    head = [argv[0]] if "/" in argv[0] else []
    return [
        token
        for token in head + argv[1:]
        if not token.startswith("-")
        and ("/" in token or token.endswith((".sh", ".py", ".mjs", ".bash")))
    ]


def _absent_script(argv: list[str]) -> str:
    """The script the caller's command names that this MERGED TREE does not hold.

    A branch whose head and base both fork from before the check script landed
    carries no such file, so `bash <it>` exits 127 and reads as a missing tool. It
    is neither: that branch configured no check, and it cannot add one — the file
    it lacks is on the default branch, which is not its base."""
    return next(
        (token for token in _named_scripts(argv) if not Path(token).exists()), ""
    )


def _fails_on_its_own(argv: list[str], sha: str) -> bool:
    """Does this parent alone fail the same check, in a scratch worktree?

    Only a 1-to-125 status counts. A parent whose check cannot RUN there (a missing
    tool in the scratch tree) reports nothing about who owns the failure."""
    with tempfile.TemporaryDirectory() as scratch:
        tree = str(Path(scratch) / "parent")
        git("worktree", "add", "--detach", tree, sha)
        try:
            done = subprocess.run(  # noqa: S603
                argv, cwd=tree, capture_output=True, text=True, check=False
            )
        finally:
            git("worktree", "remove", "--force", tree)
    return 0 < done.returncode < _NEVER_RAN


def _owners_of_the_failure(argv: list[str], head_sha: str, base_sha: str) -> list[str]:
    """The parents that fail this check on their own, so the merge is not the cause."""
    return [
        name
        for name, sha in (
            ("the base branch", base_sha),
            ("this pull request's head", head_sha),
        )
        if sha and _fails_on_its_own(argv, sha)
    ]


def run(
    *,
    untrusted_head: bool,
    repair: Callable[[Path], bool] | None = None,
    head_sha: str = "",
    base_sha: str = "",
) -> None:
    """Run the caller's check over the merged tree, and refuse to bundle when it
    fails.

    A FORK head runs none, for the reason the pre-pass runs none there: the
    command is a script that head's manifest defines, and the resolve job holds
    every model credential. An unset command is a caller that declared no check,
    and never a guess at one. ``repair`` is one bounded model pass over the merged
    tree: given the check's own report it returns whether a pass ran, and this
    re-runs the check to judge what the pass wrote."""
    if untrusted_head:
        return
    argv = shlex.split(os.environ.get("AUTO_RESOLVE_POST_MERGE_CHECK", ""))
    if not argv:
        return
    named = shlex.join(argv)
    if absent := _absent_script(argv):
        print(
            f"::notice::the post-merge check `{named}` names `{absent}`, which the "
            "merged tree does not contain — both parents fork from before that "
            "script landed, so this branch has no check configured."
        )
        return
    # Twice at most: the check, then the check again over what one repair pass
    # wrote. A LOOP rather than a second call site, so both attempts meet the same
    # three verdict gates below — a re-run reached past them is a check whose
    # second invocation stages a file every confinement and lint check already ran.
    for attempt in range(2):
        before = _tree_state()
        done = _read_the_tree(argv)
        _refuse_a_writing_check(named, before)
        _refuse_a_check_that_never_ran(named, done)
        if done.returncode == 0:
            return
        # This check is the one reader that sees the merge as a PROGRAM, so its red
        # is usually a file git text-merged into something that does not run. The
        # repair pass fixes exactly that class.
        if attempt or repair is None or not repair(_report_of(done)):
            break
    if owners := _owners_of_the_failure(argv, head_sha, base_sha):
        owned = " and ".join(owners)
        fail(
            f"`{named}` already fails on {owned}, so the merge is not the cause",
            f"the merged tree does not pass this repository's post-merge check "
            f"(`{named}`) — and neither does {owned}, on its own, with no merge "
            "involved. The conflict resolution is not the cause. Fix the check "
            "there, and the next run resolves this conflict.",
            report=report_block(done.stdout + done.stderr),
            closing=PARENT_ALREADY_FAILS,
        )
    fail(
        "the merged tree fails the caller's post-merge check "
        f"(`{named}` exited {done.returncode})",
        f"the merged tree does not pass this repository's post-merge check "
        f"(`{named}`). A merge that keeps both sides' definition of one name "
        "raises no conflict, so this "
        "check is the only thing that reads the merge as a program. When the same "
        "error is already on the head before the merge, fix it on the branch and "
        "the next run resolves the conflict.",
        report=report_block(done.stdout + done.stderr),
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
