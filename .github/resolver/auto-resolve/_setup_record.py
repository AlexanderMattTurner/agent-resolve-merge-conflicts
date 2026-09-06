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

SECOND PROBLEM CLASS — the command reads a tree the merge left CONFLICTED. The
merged worktree still carries `<<<<<<<` markers, which is the whole point of the
run, and a setup command that sources or executes one of those files meets them
as syntax. Bash dies on `syntax error near unexpected token`, the step fails, and
no resolution is ever attempted. So every conflicted path is SHIELDED for the
duration of the command: the worktree holds one parent's marker-free content
while the command runs, and the markers go back afterwards. The shield is put
back even when the command fails, because a tree left holding one parent's
content is a tree a later step could commit as a resolution nobody wrote.

Run as `_setup_record.py run`, which reads the command from
`AUTO_RESOLVE_SETUP_COMMAND` and wraps it. ONE invocation, so the shield and the
sample cannot be left half-applied by a step that dies between two of them.
bundle.py imports :func:`undo_setup_changes`.
"""

import json
import os
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
    git_lines,
)
from _refusal import fail  # noqa: E402  # pylint: disable=wrong-import-position

_RECORD_ENV = "AUTO_RESOLVE_SETUP_RECORD"
_COMMAND_ENV = "AUTO_RESOLVE_SETUP_COMMAND"

# Which parent's content a shielded path holds while the command runs, in the
# order tried. Stage 2 is the side the merge started from, so it is the one a
# caller's own scripts were written against. Stage 3 covers a path that side
# deleted, and stage 1 the merge base. A conflicted path always has one of them.
_STAGE_ORDER = ("2", "3", "1")
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


def _stage_modes() -> dict[str, dict[str, str]]:
    """Every conflicted path, mapped to the file mode each of its stages has.

    `git ls-files -u` prints `<mode> <sha> <stage>\t<path>`, one line per stage.
    """
    stages: dict[str, dict[str, str]] = {}
    for line in git_lines("ls-files", "-u"):
        meta, _, name = line.partition("\t")
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


def _write(name: str, content: bytes, mode: str) -> None:
    path = bound_repo() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        path.unlink()
    if mode == _SYMLINK_MODE:
        os.symlink(content.decode("utf-8"), path)
        return
    path.write_bytes(content)
    path.chmod(0o755 if mode == _EXECUTABLE_MODE else 0o644)


def _restore(name: str, snapshot: dict) -> None:
    path = bound_repo() / name
    if path.is_symlink() or path.exists():
        path.unlink()
    if snapshot["kind"] == "symlink":
        os.symlink(snapshot["target"], path)
    elif snapshot["kind"] == "file":
        path.write_bytes(snapshot["content"])
        path.chmod(snapshot["mode"] & 0o777)


def shield_conflicts() -> dict[str, dict]:
    """Give every conflicted path one parent's content, and return the undo.

    The command runs against a tree with no `<<<<<<<` in it, so a file it
    sources, executes or parses is readable whatever the merge did to it.
    """
    shielded: dict[str, dict] = {}
    for name, modes in _stage_modes().items():
        for stage in _STAGE_ORDER:
            if stage not in modes:
                continue
            content = git_bytes("cat-file", "blob", f":{stage}:{name}")
            if content is None:
                continue
            shielded[name] = _snapshot(name)
            _write(name, content, modes[stage])
            break
    if shielded:
        print(f"shielded {len(shielded)} conflicted path(s) from the setup command")
    return shielded


def unshield_conflicts(shielded: dict[str, dict]) -> None:
    """Put every shielded path's conflicted content back, markers and all."""
    for name, snapshot in shielded.items():
        _restore(name, snapshot)


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


def run_setup_command() -> int:
    """Run the caller's command, shielded and sampled, and return its status.

    `-eo pipefail` gives the inner shell the posture the runner gave the outer
    one. Without it only the LAST command's status escapes, so a `;`-joined
    command whose first half died reports success and the model meets exactly
    the tree this step exists to repair.
    """
    command = os.environ.get(_COMMAND_ENV, "")
    if not command:
        print(f"::error::{_COMMAND_ENV} is unset", file=sys.stderr)
        return 1
    shielded = shield_conflicts()
    try:
        capture_before()
        done = subprocess.run(
            ["bash", "-eo", "pipefail", "-c", command],
            cwd=bound_repo(),
            check=False,
        )
        if done.returncode != 0:
            return done.returncode
        capture_after()
    finally:
        unshield_conflicts(shielded)
    return 0


def main(argv: list[str]) -> None:
    if len(argv) != 2 or argv[1] != "run":
        print("::error::usage: _setup_record.py run", file=sys.stderr)
        raise SystemExit(1)
    bind_repo(Path.cwd())
    raise SystemExit(run_setup_command())


if __name__ == "__main__":
    main(sys.argv)
