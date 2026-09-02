"""What each side did to a conflicted path since the merge base.

A shard has no Bash and is told not to run git, so the prompt carries this text
instead of letting the run derive it.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompts import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    keep_both_ends,
)

# Per-side history a shard prompt carries. Bounded: the subjects are
# attacker-influencable text and a long log would crowd out the conflict.
#
# PER SIDE, not over the rendered pair: one cap over both sections is spent by the
# first, so a PR side with long subjects drops the base side entirely — and the
# prompt then reads as a base side that touched the path in no commit.
_HISTORY_MAX_COMMITS = 20
_HISTORY_MAX_CHARS_PER_SIDE = 2000


def run_git(*args: str) -> subprocess.CompletedProcess:
    # cwd-git-ok: every caller READS (merge-base, log, show, diff); this step owns
    #   its checkout, and _relocation.py reads the mid-merge tree through this too.
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def conflict_history(file: str) -> str:
    """What each side DID to this path since the merge base, as two commit
    lists. Without it the resolver judges intent from merged text alone and
    can only refuse and leave markers. It has no Bash and is told not to run
    git, so the history is handed to it. Read from the mid-merge tree: HEAD is
    the PR side, MERGE_HEAD the base side. Best-effort but loud."""
    base = run_git("merge-base", "HEAD", "MERGE_HEAD")
    if base.returncode != 0:
        print(
            f"::warning::could not derive the merge base for {file}; "
            "resolving it without per-side history.",
            file=sys.stderr,
        )
        return "unavailable (this run could not read the merge base)"
    merge_base = base.stdout.strip()

    def side(ref: str) -> str:
        # --no-merges: a merge commit's subject names the branch, not this.
        done = run_git(
            "log",
            "--no-merges",
            f"--max-count={_HISTORY_MAX_COMMITS}",
            "--format=  %h %s",
            f"{merge_base}..{ref}",
            "--",
            file,
        )
        listed = done.stdout.strip("\n") or "  (no commits touched this path)"
        return keep_both_ends(listed, _HISTORY_MAX_CHARS_PER_SIDE)

    return (
        f"On the PR side (HEAD):\n{side('HEAD')}\n\n"
        f"On the base side (MERGE_HEAD):\n{side('MERGE_HEAD')}"
    )
