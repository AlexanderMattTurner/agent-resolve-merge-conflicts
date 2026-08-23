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
from _refusal import fail  # noqa: E402,I001  # pylint: disable=wrong-import-position


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

    `check=False` catches a non-zero EXIT and nothing else, and no shell stands
    between this and the command: a binary the runner lacks raises here instead
    of reporting 127, so `_refuse_a_check_that_never_ran` below never sees that
    case. An uncaught raise loses a resolution the model has already billed for.
    """
    try:
        done = subprocess.run(argv, check=False, capture_output=True, text=True)
    except OSError as exc:
        # resolver_fault leaves the head UNMARKED, so a re-run after the caller
        # installs the tool checks this same resolution instead of waiting out
        # the attempt mark's TTL.
        fail(
            f"the post-merge check '{argv[0]}' will not run on this runner",
            f"this run resolved the conflict and then could not check the merged "
            f"tree: `post-merge-check-command` starts with `{argv[0]}`, which "
            f"this job never installs ({exc}). Nothing was landed, and the "
            "resolution is not lost — install it in the calling workflow and "
            "re-run, and this same head resolves.",
            resolver_fault=True,
        )
    print(done.stdout + done.stderr, end="")
    sys.stdout.flush()
    return done


def run(*, untrusted_head: bool, repair: Callable[[Path], bool] | None = None) -> None:
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
    # Twice at most: the check, then the check again over what one repair pass
    # wrote. A LOOP rather than a second call site, so both attempts meet the same
    # three verdict gates below — a re-run reached past them is a check whose
    # second invocation stages a file every confinement and lint check already ran.
    for attempt in range(2):
        before = _tree_state()
        done = _read_the_tree(argv)
        _refuse_a_writing_check(named, before)
        _refuse_a_check_that_never_ran(named, done.returncode)
        if done.returncode == 0:
            return
        # This check is the one reader that sees the merge as a PROGRAM, so its red
        # is usually a file git text-merged into something that does not run. The
        # repair pass fixes exactly that class.
        if attempt or repair is None or not repair(_report_of(done)):
            break
    fail(
        "the merged tree fails the caller's post-merge check "
        f"(`{named}` exited {done.returncode})",
        f"the merged tree does not pass this repository's post-merge check "
        f"(`{named}`) — the resolver job log holds what it reported. A merge that "
        "keeps both sides' definition of one name raises no conflict, so this "
        "check is the only thing that reads the merge as a program. When the same "
        "error is already on the head before the merge, fix it on the branch and "
        "the next run resolves the conflict.",
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


def _refuse_a_check_that_never_ran(named: str, code: int) -> None:
    """No mark and no blame on the merge: the fix lands in this job's provisioning,
    and a re-run against the same head then answers differently. Marking it would
    strand the head until someone pushed."""
    if code < _NEVER_RAN:
        return
    fail(
        f"the caller's post-merge check could not RUN (`{named}` exited "
        f"{code}), so nothing judged the merged tree",
        f"the merged tree could NOT be checked: `{named}` exited "
        f"{code}, which means it never ran — a missing tool, or a "
        "signal that killed it. That is a defect in this workflow's "
        "provisioning, **not** a problem with the resolution or with your "
        "branch. See the resolver job log for what failed to start.",
        resolver_fault=True,
    )


def _report_of(done: subprocess.CompletedProcess) -> Path:
    """The check's own output, on disk, for the repair pass to fix FOR."""
    handle, name = tempfile.mkstemp()
    os.close(handle)
    report = Path(name)
    report.write_text(done.stdout + done.stderr, encoding="utf-8")
    return report
