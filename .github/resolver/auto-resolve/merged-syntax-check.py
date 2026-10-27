#!/usr/bin/env python3
"""The post-merge check a caller gets when it names none: every file the merge
brought into the pull request must still parse.

PROBLEM CLASS — a resolution that no longer parses, in a repository that wired no
`post-merge-check-command`. Each file is read by the parser that owns its format
(`_hunk_separable.parser_for`): `ast`, `json`, `node --check`, `bash -n`. A format no
parser owns is skipped. Run with the merge commit checked out, so `HEAD^1` is the
pull request's head; a checkout that is not a merge has no such set and passes.
Exit 1 lists each file that does not parse, one per line.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _hunk_separable import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    parser_for,
)


def _git(*args: str) -> subprocess.CompletedProcess:
    # cwd-git-ok: the post-merge check sets cwd to the tree this judges
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def merged_paths() -> list[str]:
    """The files the merge commit at HEAD added or changed relative to the pull
    request's head, or none when HEAD is not a merge."""
    if _git("rev-parse", "--verify", "--quiet", "HEAD^2").returncode != 0:
        return []
    done = _git("diff", "--name-only", "--diff-filter=AM", "-z", "HEAD^1", "HEAD")
    if done.returncode != 0:
        raise SystemExit(f"git diff HEAD^1 HEAD failed: {done.stderr.strip()}")
    return [path for path in done.stdout.split("\0") if path]


def unparsed(paths: list[str]) -> list[str]:
    """The PATHS whose format has a parser here that rejects the merged bytes."""
    broken = []
    for path in paths:
        oracle = parser_for(path)
        file = Path(path)
        if oracle is None or not file.is_file():
            continue
        if not oracle(file.read_text(encoding="utf-8", errors="replace")):
            broken.append(path)
    return broken


def main() -> None:
    broken = unparsed(merged_paths())
    for path in broken:
        print(f"{path}: the merged file does not parse")
    if broken:
        sys.exit(1)


if __name__ == "__main__":
    main()
