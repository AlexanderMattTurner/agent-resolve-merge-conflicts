#!/usr/bin/env python3
"""What the caller's `setup-command` changed, so the merge commit carries none of it.

PROBLEM CLASS — a repair the AGENT needs is not part of the RESOLUTION, and the
bundle step cannot tell the two apart by looking at the tree. `setup-command`
runs before the model to make the checkout one an agent can start in — pruning a
tracked symlink that dangles in CI, for one — and every path it touches then
reads to `bundle.refuse_edits_outside_the_set` as a file the model edited outside
the conflicted set. That aborts a paid resolution and blames the model for it.

So the tree is sampled TWICE around the command, inside the step that runs it: a
change is attributed by WHEN it appeared, never by what it looks like. The undo
below restores exactly those paths before the bundle step judges the tree, and
the model — which runs after the sample — can add nothing to the record.

A path the setup touched and the model then edited is NOT restored. It is a model
edit outside the conflicted set wearing a setup change's clothes, so it aborts.

Run as `_setup_record.py before|after` around the command. bundle.py imports
:func:`undo_setup_changes`.
"""

import json
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    bind_repo,
    bound_repo,
    git,
    git_lines,
)
from _refusal import fail  # noqa: E402  # pylint: disable=wrong-import-position

_RECORD_ENV = "AUTO_RESOLVE_SETUP_RECORD"


def _worktree_hash(name: str) -> str | None:
    """This path's current content, or None when it is absent.

    A symlink is read as its TARGET rather than hashed: the case this whole
    module exists for is a symlink that dangles, which no `hash-object` can open.
    """
    path = bound_repo() / name
    if path.is_symlink():
        return f"symlink:{os.readlink(path)}"
    if not path.exists():
        return None
    return git("hash-object", "--", name).strip()


def _sample() -> dict:
    """The three facts a setup change can move."""
    unmerged = sorted({line.split("\t")[-1] for line in git_lines("ls-files", "-u")})
    return {
        "dirty": sorted(git_lines("diff", "--name-only")),
        "untracked": sorted(git_lines("ls-files", "--others", "--exclude-standard")),
        "unmerged": {name: _worktree_hash(name) for name in unmerged},
    }


def _record_path() -> Path:
    path = os.environ.get(_RECORD_ENV, "")
    if not path:
        print(f"::error::{_RECORD_ENV} is unset", file=sys.stderr)
        raise SystemExit(1)
    return Path(path)


def capture_before() -> None:
    _record_path().write_text(json.dumps({"before": _sample()}), encoding="utf-8")


def capture_after() -> None:
    record = _record_path()
    before = json.loads(record.read_text(encoding="utf-8"))["before"]
    now = _sample()
    # A conflicted file the setup rewrote has no honest reading later: the model
    # resolves that same file next, so nothing downstream could separate the two
    # edits. Refuse here, where the run has spent nothing yet.
    rewritten = [
        name
        for name, digest in before["unmerged"].items()
        if _worktree_hash(name) != digest
    ]
    if rewritten:
        print(
            "::error::the setup command rewrote conflicted file(s) "
            f"({', '.join(rewritten)}); it may only prepare files the resolution "
            "does not touch",
            file=sys.stderr,
        )
        raise SystemExit(1)
    entries = [
        {"path": name, "kind": "tracked", "hash": _worktree_hash(name)}
        for name in sorted(set(now["dirty"]) - set(before["dirty"]))
    ] + [
        {"path": name, "kind": "untracked", "hash": _worktree_hash(name)}
        for name in sorted(set(now["untracked"]) - set(before["untracked"]))
    ]
    record.write_text(json.dumps({"entries": entries}), encoding="utf-8")


def undo_setup_changes() -> None:
    """Put every setup-touched path back, so the tree reads as if it never ran.

    A no-op when the caller named no setup command: the step that writes the
    record does not run, so there is no record to read.
    """
    path = os.environ.get(_RECORD_ENV, "")
    if not path or not Path(path).is_file():
        return
    entries = json.loads(Path(path).read_text(encoding="utf-8")).get("entries", [])
    for entry in entries:
        name = entry["path"]
        if _worktree_hash(name) != entry["hash"]:
            fail(
                f"the setup command prepared '{name}' and the resolution then "
                "changed it",
                f"`{name}` is not in the conflicted set. The setup command "
                "touched it before the model ran, and its content moved again "
                "afterwards, so this is an edit outside the set.",
            )
        if entry["kind"] == "untracked":
            (bound_repo() / name).unlink(missing_ok=True)
        else:
            # Restores from the INDEX, which still holds the merge's own answer
            # for this path: the setup command changed the worktree only. Safe
            # mid-merge because a path with conflict stages never reaches here —
            # `capture_after` refuses one.
            git("checkout", "--", name)


def main(argv: list[str]) -> None:
    if len(argv) != 2 or argv[1] not in ("before", "after"):
        print("::error::usage: _setup_record.py before|after", file=sys.stderr)
        raise SystemExit(1)
    bind_repo(Path.cwd())
    if argv[1] == "before":
        capture_before()
    else:
        capture_after()


if __name__ == "__main__":
    main(sys.argv)
