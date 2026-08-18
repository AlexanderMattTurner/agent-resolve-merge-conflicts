"""Helpers the ported auto-resolve suites need, kept OUT of `_helpers.py`.

`tests/_helpers.py` arrives from the template repo through template-sync, so a
symbol added there conflicts on every sync. These came across with the resolver
tests from agent-glovebox and belong to the resolver, so they live beside it.

`REPO_ROOT`, `git_env` and `run_capture` are re-exported from `_helpers` rather
than re-implemented, so there stays one definition of each.
"""

import atexit
import fnmatch
import importlib.util as importlib_util
import os
import re
import shutil
import subprocess
import tempfile
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

from tests._helpers import REPO_ROOT, git_env, run_capture

__all__ = [
    "REPO_ROOT",
    "SYSTEM_PATH_DIRS",
    "copy_tracked_tree",
    "current_path",
    "git_env",
    "load_script",
    "load_script_module",
    "path_without_binary",
    "read_github_outputs",
    "record_gh_call",
    "run_capture",
    "status_comments",
    "tracked_paths",
]


def current_path() -> str:
    """The live PATH, so a hermetic test env can still resolve git/bash."""
    return os.environ.get("PATH", "/usr/bin:/bin")


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


# The tool dirs a harness restricts itself to when it wants only the system
# toolchain on PATH.
SYSTEM_PATH_DIRS = ("/usr/bin", "/bin")

_shadow_root: Path | None = None
# Keyed on (dirs, hidden): a farm is only reusable for the exact PATH it shadows,
# so a caller that edits `os.environ["PATH"]` cannot be handed another's.
_shadow_dirs: dict[tuple[tuple[str, ...], tuple[str, ...]], str] = {}


def _link_farm(dest: Path, dirs, hidden) -> Path:
    """Fill `dest` with symlinks to every executable `dirs` holds except `hidden`,
    earlier dirs winning — PATH's own first-match rule.

    The `lexists` guard (rather than a name set) survives a case-insensitive
    checkout, where two PATH entries differing only in case map to one path and
    the second symlink would collide.
    """
    dest.mkdir(parents=True, exist_ok=True)
    excluded = set(hidden)
    for d in dirs:
        src = Path(d)
        if not src.is_dir():
            continue
        for entry in src.iterdir():
            target = dest / entry.name
            if entry.name in excluded or os.path.lexists(target):
                continue
            if os.access(entry, os.X_OK):
                target.symlink_to(entry)
    return dest


def _cached_farm(dirs: tuple[str, ...], hidden: tuple[str, ...]) -> str:
    """A `_link_farm` over `dirs` minus `hidden`, built ONCE PER PROCESS per
    (dirs, hidden) and shared by every caller asking for the same one — one farm
    is a few thousand filesystem calls, and a harness asks for the same one once
    per test.

    Sharing is safe because NOTHING WRITES INTO A FARM: it holds only symlinks to
    host tools, and a test needing its own writable dir in front passes one as a
    `prefix_dirs` entry instead.
    """
    global _shadow_root
    # Canonical key: two spellings of the same names build identical farms, so a
    # reordered argument list must not cost a whole extra PATH walk.
    hidden = tuple(sorted(set(hidden)))
    key = (dirs, hidden)
    cached = _shadow_dirs.get(key)
    if cached is not None:
        return cached
    if _shadow_root is None:
        _shadow_root = Path(tempfile.mkdtemp(prefix="resolver-shadow-path-"))
        atexit.register(shutil.rmtree, _shadow_root, True)
    farm = str(_link_farm(_shadow_root / str(len(_shadow_dirs)), dirs, hidden))
    _shadow_dirs[key] = farm
    return farm


def _shadow_dir(src: Path, hidden: frozenset[str]) -> str:
    """One PATH entry's stand-in: `src` minus `hidden`."""
    return _cached_farm((str(src),), tuple(hidden))


def path_without_binary(binary, *prefix_dirs: str | Path, base=None) -> str:
    """A PATH with `binary` (one name, or an iterable of names hidden together)
    made unresolvable, so a `command -v <binary>` guard fires deterministically
    whatever the runner has installed — while every OTHER tool stays exactly as
    resolvable. `prefix_dirs` go in front (the caller's stub dirs); `base` is the
    dir list to hide the name in, defaulting to the host's real PATH.

    A directory carrying `binary` is not dropped wholesale: `timeout` shares
    /usr/bin with the coreutils a stub still needs. Instead the dir is replaced,
    in its own PATH position, by a shadow directory of symlinks to everything it
    holds except `binary`, so resolution order for the rest is unchanged.

    Several names must be hidden in ONE call: a resolver accepting either spelling
    (`timeout` / `gtimeout`) is only unresolvable when both go at once, and a
    second pass would rebuild its shadows from the unmodified base.
    """
    hidden = frozenset({binary} if isinstance(binary, str) else binary)
    entries = os.environ["PATH"].split(":") if base is None else base
    kept: list[str] = []
    for p in entries:
        if not p:
            continue
        src = Path(p)
        carries = src.is_dir() and any((src / name).exists() for name in hidden)
        kept.append(_shadow_dir(src, hidden) if carries else p)
    return ":".join([str(d) for d in prefix_dirs] + kept)
