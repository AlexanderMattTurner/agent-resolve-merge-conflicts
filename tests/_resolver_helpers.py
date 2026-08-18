"""Helpers the ported auto-resolve suites need, kept OUT of `_helpers.py`.

`tests/_helpers.py` arrives from the template repo through template-sync, so a
symbol added there conflicts on every sync. These came across with the resolver
tests from agent-glovebox and belong to the resolver, so they live beside it.

`REPO_ROOT`, `git_env` and `run_capture` are re-exported from `_helpers` rather
than re-implemented, so there stays one definition of each.
"""

import fnmatch
import importlib.util as importlib_util
import os
import re
import shutil
import subprocess
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

from tests._helpers import REPO_ROOT, git_env, run_capture

__all__ = [
    "REPO_ROOT",
    "copy_tracked_tree",
    "git_env",
    "load_script",
    "load_script_module",
    "read_github_outputs",
    "record_gh_call",
    "run_capture",
    "status_comments",
    "tracked_paths",
]


def load_script_module(name: str, path: Path) -> types.ModuleType:
    """Import the script at PATH under module NAME so its functions can be driven
    in-process, whatever its filename — a hyphenated `.github/resolver/**/*.py` is
    not a legal import name. Naming the loader explicitly (rather than deriving one
    from the path) is what makes the spec unconditional, so the caller gets a
    module or an exception, never a silently half-built one."""
    loader = SourceFileLoader(name, str(path))
    spec = importlib_util.spec_from_loader(loader.name, loader)
    # A SourceFileLoader always yields a spec; None means the import machinery
    # refused this path, and executing nothing would hand back an empty module
    # whose missing attributes surface much later as a confusing AttributeError.
    if spec is None:
        raise ImportError(f"no module spec for {path}")
    module = importlib_util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_script(rel: str) -> types.ModuleType:
    """Import the script at REPO_ROOT/<rel> in-process (see load_script_module),
    under a module name derived from the filename: suffix dropped, every
    non-identifier character mapped to `_` (`auto-resolve/run-ladder.py` ->
    run_ladder)."""
    path = REPO_ROOT / rel
    return load_script_module(re.sub(r"\W", "_", path.stem), path)


def tracked_paths(rel: str, root: Path = REPO_ROOT) -> list[str]:
    """Root-relative paths git tracks under <root>/<rel> that are ALSO present in
    the working tree.

    `git ls-files` reads the INDEX, so it still lists a tracked file the working
    tree has deleted. Every caller here reads working-tree content, so an unstaged
    deletion must read as absent rather than as a path that then fails to open.
    """
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", rel],
        capture_output=True,
        text=True,
        check=True,
    )
    # lexists, not exists: a tracked symlink whose target is missing is still present.
    return [p for p in out.stdout.split("\0") if p and os.path.lexists(root / p)]


def copy_tracked_tree(
    rel: str, dst: Path, *, ignore_patterns: tuple[str, ...] = ()
) -> None:
    """Copy REPO_ROOT/<rel> into <dst>, including ONLY git-tracked entries (symlinks
    and mode bits preserved). `ignore_patterns` are `fnmatch` globs matched against
    each entry's NAME, for a caller that wants a tracked subset. Uses the
    working-tree content (not `git archive`), so uncommitted edits to tracked files
    are still reflected.

    `shutil.copytree` lists a directory once and then copies from that list, so an
    entry a concurrent pytest-xdist worker removes between the two steps kills the
    whole copy. `git ls-files` never lists an untracked scratch entry, so it cannot
    race the copy at all.
    """
    for relpath in tracked_paths(rel):
        src = REPO_ROOT / relpath
        subpath = Path(relpath).relative_to(rel)
        if any(
            fnmatch.fnmatch(part, pattern)
            for part in subpath.parts
            for pattern in ignore_patterns
        ):
            continue
        target = dst / subpath
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            target.symlink_to(os.readlink(src))
        else:
            shutil.copy2(src, target, follow_symlinks=False)


def read_github_outputs(path):
    """The `$GITHUB_OUTPUT` file at PATH as the runner reads it: plain
    `key=value` lines, plus `key<<DELIM` heredoc blocks that run to their
    closing DELIM. An unterminated heredoc or a delimiterless line fails the
    test rather than parsing as an empty value."""
    outputs, lines = {}, path.read_text(encoding="utf-8").splitlines()
    while lines:
        line = lines.pop(0)
        key, sep, rest = line.partition("<<")
        if sep:
            body = []
            while lines and lines[0] != rest:
                body.append(lines.pop(0))
            assert lines, f"unterminated heredoc for {key!r}"
            lines.pop(0)
            outputs[key] = "\n".join(body)
            continue
        key, sep, value = line.partition("=")
        assert sep, f"unreadable GITHUB_OUTPUT line: {line!r}"
        outputs[key] = value
    return outputs


def record_gh_call(log: str, prefix: str = "$*") -> str:
    """Bash that appends the current `gh` call to LOG, for a shim on PATH.

    A status comment's body reaches `gh` in a FILE (`-F body=@path`), so an argv-only
    recording says nothing about what the pull request is told — and what it is told is
    what several auto-resolve fixtures assert. This expands the file into the same line
    and flattens the body's newlines, so one gh call stays one recorded line.

    The .mjs suites over the same scripts share
    `.github/resolver/auto-resolve/_gh-shim.mjs`, which emits the same bash.
    """
    return (
        f'line="{prefix}"\n'
        'for arg in "$@"; do\n'
        '  [[ "$arg" == body=@* ]] && line+=" $(cat "${arg#body=@}")"\n'
        "done\n"
        f"printf '%s\\n' \"${{line//$'\\n'/ }}\" >>\"{log}\"\n"
    )


def status_comments(log_text: str) -> list[str]:
    """The recorded calls that posted or rewrote the PR's auto-resolve status comment."""
    return [line for line in log_text.splitlines() if "body=@" in line]
