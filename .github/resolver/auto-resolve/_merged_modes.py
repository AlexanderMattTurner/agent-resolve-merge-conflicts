"""PROBLEM CLASS — a merge stages a file mode, such as an executable bit, that no
parent asked for.

A shard that replaces a file rather than editing it in place leaves the default
mode, so a script both parents carry as 100755 lands as 100644 with identical
bytes. No content check sees the change (agent-glovebox#6891, where only the
caller's shebang lint caught it, after the push).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    bound_repo,
    git,
    git_result,
)
from _taken_whole import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    tree_entry,
)

_REGULAR = {"100644": "-x", "100755": "+x"}


def _mode(rev: str, path: str) -> str | None:
    entry = tree_entry(rev, path)
    return entry.split()[0] if entry else None


def merged_mode(base: str | None, ours: str | None, theirs: str | None) -> str | None:
    """The mode a three-way merge gives a path: the side that changed it wins, and
    None when both changed it differently, which only a person can settle."""
    if ours == base:
        return theirs
    if theirs in (base, ours):
        return ours
    return None


def _staged_mode(path: str) -> str | None:
    entry = git("ls-files", "-s", "-z", "--", f":(literal){path}").split("\0")[0]
    meta = entry.split("\t", 1)[0].split()
    # Stage 0 only: an unmerged path has no settled mode to correct.
    return meta[0] if len(meta) == 3 and meta[2] == "0" else None


def restore_merged_modes(paths: list[str], ours: str, theirs: str) -> list[str]:
    """Re-stage each of PATHS whose staged regular-file mode differs from the mode
    the merge of OURS and THEIRS gives it, and return the paths corrected.

    Only the executable bit moves: a symlink or submodule mode is never rewritten.
    """
    done = git_result("merge-base", "--all", ours, theirs)
    if done.returncode != 0 or len(done.stdout.split()) != 1:
        return []
    base = done.stdout.strip()
    corrected = []
    for path in paths:
        wanted = merged_mode(_mode(base, path), _mode(ours, path), _mode(theirs, path))
        staged = _staged_mode(path)
        if wanted == staged or wanted not in _REGULAR or staged not in _REGULAR:
            continue
        git("update-index", f"--chmod={_REGULAR[wanted]}", "--", path)
        # The worktree follows the index, so a later `git add` cannot undo this.
        file = bound_repo() / path
        current = file.stat().st_mode
        file.chmod(current | 0o111 if wanted == "100755" else current & ~0o111)
        corrected.append(path)
    return corrected
