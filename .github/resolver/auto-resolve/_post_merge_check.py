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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _refusal import fail  # noqa: E402,I001  # pylint: disable=wrong-import-position


def run(*, untrusted_head: bool) -> None:
    """Run the caller's check over the merged tree, and refuse to bundle when it
    fails.

    A FORK head runs none, for the reason the pre-pass runs none there: the
    command is a script that head's manifest defines, and the resolve job holds
    every model credential. An unset command is a caller that declared no check,
    and never a guess at one. Stdio is inherited, so the check's own report lands
    in the resolver job log in the order it wrote it."""
    if untrusted_head:
        return
    argv = shlex.split(os.environ.get("AUTO_RESOLVE_POST_MERGE_CHECK", ""))
    if not argv:
        return
    done = subprocess.run(argv, check=False)
    if done.returncode == 0:
        return
    named = shlex.join(argv)
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
