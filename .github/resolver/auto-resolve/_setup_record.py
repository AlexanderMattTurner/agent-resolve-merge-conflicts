#!/usr/bin/env python3
"""What the caller's `setup-command` changed, so the merge commit carries none of it.

PROBLEM CLASS — a repair the AGENT needs is not part of the RESOLUTION, and the
bundle step cannot tell the two apart by looking at the tree. So the tree is
sampled TWICE around the command: a change is attributed by WHEN it appeared,
never by what it looks like. :func:`undo_setup_changes`, which bundle.py imports,
restores exactly those paths before the bundle step judges the tree. A path the
setup touched and the model then edited is NOT restored: that is an edit outside
the conflicted set, so it aborts.

SECOND PROBLEM CLASS — the command reads a tree the merge left CONFLICTED. A
setup command that sources or executes a conflicted file meets `<<<<<<<` as
syntax, bash dies, and no resolution is attempted. So every conflicted path is
SHIELDED: it holds one parent's marker-free content while the command runs, and
the markers go back afterwards, failure included.

Run as `_setup_record.py run`, which reads the command from
`AUTO_RESOLVE_SETUP_COMMAND` and wraps it. ONE invocation, so the shield and the
samples cannot be left half-applied by a step that dies between two of them.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    bind_repo,
    bound_repo,
    git,
    git_bytes,
)
from _refusal import fail  # noqa: E402  # pylint: disable=wrong-import-position

_RECORD_ENV = "AUTO_RESOLVE_SETUP_RECORD"
_COMMAND_ENV = "AUTO_RESOLVE_SETUP_COMMAND"

# A path with BOTH sides recorded is the one git writes markers into. Every
# other conflict — a modify/delete, an unmerged path one side never had — leaves
# the worktree holding one parent's content already, with nothing to hide.
_OURS = "2"
_THEIRS = "3"
_SYMLINK_MODE = "120000"
_EXECUTABLE_MODE = "100755"


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


def _paths(*args: str) -> list[str]:
    """One git call's PATHS, sorted, read through `-z`.

    Every path this module compares comes through here, so a quoted name cannot
    make one sample's spelling differ from the other's.
    """
    record = git_bytes(*args, "-z") or b""
    return sorted(
        entry.decode("utf-8", "surrogateescape")
        for entry in record.split(b"\0")
        if entry
    )


def _stage_modes() -> dict[str, dict[str, str]]:
    """Every conflicted path, mapped to the file mode each of its stages has.

    `-z` because the plain form QUOTES a path holding a non-ASCII byte, a quote
    or a newline: `git ls-files -u` prints `"caf\\303\\251.sh"`, and `cat-file
    blob :2:"caf\\303\\251.sh"` then exits 128 and the path is never shielded.
    """
    record = git_bytes("ls-files", "-u", "-z") or b""
    stages: dict[str, dict[str, str]] = {}
    for entry in record.split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.decode("utf-8", "surrogateescape").partition("\t")
        mode, _sha, stage = meta.split()
        stages.setdefault(name, {})[stage] = mode
    return stages


def _snapshot(name: str) -> dict:
    """Exactly what the worktree holds at NAME, so the shield can put it back."""
    path = bound_repo() / name
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if not path.exists():
        return {"kind": "absent"}
    return {"kind": "file", "content": path.read_bytes(), "mode": path.stat().st_mode}


def _clear(path: Path) -> None:
    """Empty this path, whatever occupies it.

    A setup command may leave a DIRECTORY where a shielded file was, and
    `unlink` on one raises inside the restore — which would strand the rest of
    the shield and mask the command's own status.
    """
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _write(name: str, content: bytes, mode: str) -> None:
    path = bound_repo() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    _clear(path)
    if mode == _SYMLINK_MODE:
        os.symlink(content.decode("utf-8"), path)
        return
    path.write_bytes(content)
    path.chmod(0o755 if mode == _EXECUTABLE_MODE else 0o644)


def _restore(name: str, snapshot: dict) -> None:
    path = bound_repo() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    _clear(path)
    if snapshot["kind"] == "symlink":
        os.symlink(snapshot["target"], path)
    elif snapshot["kind"] == "file":
        path.write_bytes(snapshot["content"])
        path.chmod(snapshot["mode"] & 0o777)


def shield_conflicts() -> dict[str, dict]:
    """Give every conflicted path one parent's content, and return the undo.

    The command runs against a tree with no `<<<<<<<` in it, so a file it
    sources, executes or parses is readable whatever the merge did to it. Only
    a both-sides conflict is touched; see `_OURS` above.
    """
    shielded: dict[str, dict] = {}
    for name, modes in _stage_modes().items():
        if _OURS not in modes or _THEIRS not in modes:
            continue
        # Stage 2 is the side the merge started from, so it is the version a
        # caller's own scripts were written against.
        content = git_bytes("cat-file", "blob", f":{_OURS}:{name}")
        if content is None:
            # A gitlink records no blob, so nothing here can shield it. Say so:
            # the alternative is the original failure with no cause named.
            print(
                f"::warning::'{name}' has no blob to shield with, so the setup "
                "command still sees whatever the merge left there",
                file=sys.stderr,
            )
            continue
        shielded[name] = _snapshot(name)
        _write(name, content, modes[_OURS])
    if shielded:
        print(
            f"shielded {len(shielded)} conflicted path(s) from the setup command",
            flush=True,
        )
    return shielded


def _sample() -> dict:
    """The three facts a setup change can move."""
    return {
        "dirty": _paths("diff", "--name-only"),
        "untracked": _paths("ls-files", "--others", "--exclude-standard"),
        "unmerged": {name: _worktree_hash(name) for name in sorted(_stage_modes())},
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
    # edits. Refuse here, where the run has spent nothing yet. Both samples read
    # the SHIELDED content, so a rewrite to exactly that content is invisible —
    # and harmless, because the restore below puts the markers back either way.
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
                f"`{name}` is not a path the resolver was handed. The setup "
                "command touched it before the model ran, and its content moved "
                "again afterwards, so this is an edit outside the set.",
            )
        if entry["kind"] == "untracked":
            (bound_repo() / name).unlink(missing_ok=True)
        else:
            # Restores from the INDEX, which still holds the merge's own answer
            # for this path: the setup command changed the worktree only. Safe
            # mid-merge because a path with conflict stages never reaches here —
            # `capture_after` refuses one.
            git("checkout", "--", name)


def run_setup_command() -> None:
    """Run the caller's command, shielded and sampled. Raises on its failure.

    `-eo pipefail` gives the inner shell the posture the runner gave the outer
    one. Without it only the LAST command's status escapes, so a `;`-joined
    command whose first half died reports success and the model meets exactly
    the tree this step exists to repair.
    """
    command = os.environ.get(_COMMAND_ENV, "")
    if not command:
        print(f"::error::{_COMMAND_ENV} is unset", file=sys.stderr)
        raise SystemExit(1)
    shielded: dict[str, dict] = {}
    try:
        shielded = shield_conflicts()
        capture_before()
        done = subprocess.run(["bash", "-eo", "pipefail", "-c", command], check=False)
        if done.returncode != 0:
            # A signalled child reports a NEGATIVE code, which `SystemExit` would
            # turn into an unrelated status. Shells spell that 128 + signal.
            code = done.returncode
            raise SystemExit(code if code > 0 else 128 - code)
        capture_after()
    finally:
        for name, snapshot in shielded.items():
            _restore(name, snapshot)


def main(argv: list[str]) -> None:
    if len(argv) != 2 or argv[1] != "run":
        print("::error::usage: _setup_record.py run", file=sys.stderr)
        raise SystemExit(1)
    bind_repo(Path.cwd())
    run_setup_command()


if __name__ == "__main__":
    main(sys.argv)
