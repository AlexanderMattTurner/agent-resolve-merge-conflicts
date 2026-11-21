"""Which generated REGIONS of a merged file a fresh generator run reproduces.

A hand-written file can hold one `BEGIN GENERATED` region, such as a workflow
whose job list a script derives. The whole-file check in
`remerge-diff-report.py` cannot retire it, because a person wrote the rest.
This check works region by region, by the same rule: re-derivation, never a
claim.

The proof is that the generator WRITES the bytes. Each region's body is emptied
in a scratch worktree at the merge, the generator it names runs there, and the
region counts as verified only when the generator puts back the exact committed
body. A region the generator never writes stays empty, so it stays in review.

A caller that holds credentials over a tree a pull request wrote names a commit
it trusts in TRUSTED_REV_ENV. A generator then runs only when that commit
declares it for the same region, and only on code byte-identical to that commit.
"""

import os
import subprocess
import sys
import tempfile
from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "auto-resolve"))

# One generator's wall-clock ceiling. A generator that overruns proves nothing,
# so its regions stay in review.
_GENERATOR_TIMEOUT_S = 120

#: The commit whose code a generator may run. Read from the environment, never
#: from the tree under review, because the tree is what this distrusts.
TRUSTED_REV_ENV = "AUTO_RESOLVE_VERIFY_REGIONS_TRUSTED_REV"


class VerifiedRegion(NamedTuple):
    """One region whose body a fresh generator run reproduced: its label, its
    generator, and the 1-based lines of its BEGIN and END markers in the merge."""

    where: str
    generator: str
    begin: int
    end: int


def _body(lines: list[str], begin: int, end: int) -> list[str]:
    return lines[begin + 1 : end]


def _runnable(tree: Path, generator: str) -> bool:
    """Whether GENERATOR is a Python file tracked inside TREE.

    The name comes from a marker line the merge wrote, so a path that leaves the
    tree, or names no tracked file, runs nothing.
    """
    if not generator.endswith(".py") or Path(generator).is_absolute():
        return False
    if ".." in Path(generator).parts:
        return False
    listed = subprocess.run(
        ["git", "-C", str(tree), "ls-files", "--stage", "--", generator],
        capture_output=True,
        text=True,
        check=False,
    )
    # A symlink (mode 120000) would run whatever it points at.
    return listed.returncode == 0 and listed.stdout.startswith("100")


def _run(tree: Path, generator: str, written_python: list[str], pinned: bool) -> bool:
    """Run GENERATOR in TREE with the credential-free environment, reporting
    whether it exited 0 inside the time limit. With WRITTEN_PYTHON, or when
    PINNED to a trusted commit, the run goes through `_generator_guard.py`,
    which fails it on any open of those files. PINNED also lets it run `git`."""
    # pylint: disable=import-outside-toplevel
    from _refusal import reap_group
    from regen_marked_regions import generator_env

    argv = [sys.executable, generator]
    if written_python or pinned:
        guard = Path(__file__).resolve().parent / "_generator_guard.py"
        argv = [
            sys.executable,
            str(guard),
            *(["--allow-git"] if pinned else []),
            generator,
        ]
    with subprocess.Popen(  # noqa: S603
        argv,
        cwd=tree,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        env=generator_env(),
        start_new_session=True,
    ) as proc:
        try:
            _out, err = proc.communicate(
                "".join(f"{path}\0" for path in written_python),
                timeout=_GENERATOR_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            err = f"timed out after {_GENERATOR_TIMEOUT_S}s"
    reap_group(proc.pid)
    if proc.returncode == 0:
        return True
    sys.stderr.write(
        f"::warning::{generator} did not re-derive its regions "
        f"(exit {proc.returncode}), so they stay in the review: {err.strip()}\n"
    )
    return False


def verified_regions(
    sha: str, paths: list[str], written: list[str]
) -> dict[str, list[VerifiedRegion]]:
    """Of PATHS at SHA, every marked region a fresh generator run reproduces.
    WRITTEN is every path the resolution wrote: the merge's delta against the
    mechanical merge of its parents.

    Opt-in through AUTO_RESOLVE_VERIFY_REGENERATED, as the whole-file check is,
    or through TRUSTED_REV_ENV alone, which runs no whole-file generator.
    A region this cannot verify is absent from the answer, so it stays in review.
    """
    trusted = os.environ.get(TRUSTED_REV_ENV, "")
    if not trusted and os.environ.get("AUTO_RESOLVE_VERIFY_REGENERATED") != "true":
        return {}
    declared = {path: _declared(sha, path) for path in paths}
    if trusted:
        # Only a generator TRUSTED itself names for the region runs, so a marker
        # cannot point the run at any other script the trusted commit carries.
        declared = {
            path: pairs & _declared(trusted, path) for path, pairs in declared.items()
        }
        # Every file that differs from TRUSTED counts as written, so the guard
        # pins the generator and each module it imports to TRUSTED's bytes.
        written = [*written, *_differs_from(trusted, sha)]
    declared = {path: pairs for path, pairs in declared.items() if pairs}
    if not declared:
        return {}
    # INVARIANT: every generator run here, and every module it imports, is code
    # a parent carried, or with TRUSTED, code TRUSTED carries. `_generator_guard.py`
    # enforces it per run: a generator that opens a `.py`/`.pyc` written or
    # differing, or starts a process other than a pinned run's `git`, verifies nothing.
    # A native module loads through dlopen, which raises no `open` audit event,
    # so the guard cannot see one: a resolution that wrote any verifies nothing.
    if any(path.endswith(tuple(EXTENSION_SUFFIXES)) for path in written):
        return {}
    written_python = [path for path in written if path.endswith((".py", ".pyc"))]
    with tempfile.TemporaryDirectory(prefix="remerge-regions-") as scratch:
        tree = Path(scratch) / "tree"
        subprocess.run(
            # cwd-git-ok: the merge under review is in the repository this runs in.
            ["git", "worktree", "add", "--detach", "--quiet", str(tree), sha],
            check=True,
        )
        try:
            return _verify_in(tree, declared, written_python, bool(trusted))
        finally:
            subprocess.run(
                # cwd-git-ok: the worktree was added from this same repository.
                ["git", "worktree", "remove", "--force", str(tree)],
                check=False,
            )


def _blob_text(rev: str, path: str) -> str | None:
    """PATH's text at REV, or None when it is absent there or not UTF-8."""
    shown = subprocess.run(
        # cwd-git-ok: both revisions are in the repository this runs in.
        ["git", "cat-file", "blob", f"{rev}:{path}"],
        capture_output=True,
        check=False,
    )
    try:
        return shown.stdout.decode("utf-8") if shown.returncode == 0 else None
    except UnicodeDecodeError:
        return None


def _declared(rev: str, path: str) -> frozenset[tuple[str, str]]:
    """Every (label, generator) pair the markers in PATH at REV declare."""
    # Imported on use, like `_run`'s imports: the sticky-comment job loads this
    # module from a sparse checkout that carries none of them. Each is the
    # RESOLVER's own copy, never the reviewed tree's.
    from lib_marked_region import marked_regions  # pylint: disable=import-outside-toplevel

    text = _blob_text(rev, path)
    if text is None:
        return frozenset()
    return frozenset((r.where, r.generator) for r in marked_regions(text))


def _differs_from(trusted: str, sha: str) -> list[str]:
    """Every path whose entry at SHA differs from TRUSTED's, either side's name."""
    listed = subprocess.run(
        # cwd-git-ok: both revisions are in the repository this runs in.
        ["git", "diff-tree", "-r", "--name-only", "-z", trusted, sha],
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in listed.stdout.split("\0") if path]


def _verify_in(
    tree: Path,
    declared: dict[str, frozenset[tuple[str, str]]],
    written_python: list[str],
    pinned: bool,
) -> dict[str, list[VerifiedRegion]]:
    from lib_marked_region import marked_regions  # pylint: disable=import-outside-toplevel

    committed: dict[str, list[str]] = {}
    candidates: dict[str, list] = {}
    for path, pairs in declared.items():
        file = tree / path
        if file.is_symlink() or not file.is_file():
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found = marked_regions(text)
        # Regions are matched by label after the run, so a repeated label is ambiguous.
        if len({r.where for r in found}) != len(found):
            continue
        regions = [
            r
            for r in found
            if (r.where, r.generator) in pairs and _runnable(tree, r.generator)
        ]
        if not regions:
            continue
        lines = text.splitlines(keepends=True)
        committed[path] = lines
        candidates[path] = regions
        emptied = list(lines)
        for region in sorted(regions, key=lambda r: r.begin, reverse=True):
            del emptied[region.begin + 1 : region.end]
        file.write_text("".join(emptied), encoding="utf-8")
    ran = {
        generator: _run(tree, generator, written_python, pinned)
        for generator in sorted({r.generator for rs in candidates.values() for r in rs})
    }
    verified: dict[str, list[VerifiedRegion]] = {}
    for path, regions in candidates.items():
        file = tree / path
        if file.is_symlink() or not file.is_file():
            continue
        after = file.read_text(encoding="utf-8").splitlines(keepends=True)
        again_found = marked_regions("".join(after))
        if len({r.where for r in again_found}) != len(again_found):
            continue
        rederived = {r.where: r for r in again_found}
        lines = committed[path]
        for region in regions:
            again = rederived.get(region.where)
            body = _body(lines, region.begin, region.end)
            # An empty body is what a generator that never writes leaves too.
            if not ran[region.generator] or again is None or not body:
                continue
            if _body(after, again.begin, again.end) != body:
                continue
            verified.setdefault(path, []).append(
                VerifiedRegion(
                    region.where, region.generator, region.begin + 1, region.end + 1
                )
            )
    return verified


def hunk_inside(hunk: str, regions: list[VerifiedRegion]) -> bool:
    """Whether every line HUNK adds or removes sits strictly between the markers
    of one of REGIONS, read on the merge's (new) side of the hunk.

    A removed line has no new-side number, so it is placed at the next new-side
    line. A marker line itself is never inside, so a hunk that edits a marker
    stays in review.
    """
    header, *body = hunk.split("\n")
    try:
        new_side = header.split("+", 1)[1].split(" ", 1)[0]
        start, _, count = new_side.partition(",")
        line = int(start)
        # With no new-side lines, `+c,0` names the line BEFORE the hunk.
        if count and int(count) == 0:
            line += 1
    except (IndexError, ValueError):
        return False
    changed = False
    for text in body:
        if text.startswith("+"):
            if not any(r.begin < line < r.end for r in regions):
                return False
            changed = True
            line += 1
        elif text.startswith("-"):
            if not any(r.begin < line <= r.end for r in regions):
                return False
            changed = True
        elif text.startswith(" "):
            line += 1
    return changed
