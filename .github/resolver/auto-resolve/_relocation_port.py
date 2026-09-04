"""Give a missed rename the three-way merge git would have done.

PROBLEM CLASS — git failed to detect a rename, so one side's edits have no
merge to take part in. A side moves a file's body to a new path and leaves a
launcher at the old one; rename detection cannot fire, because the old path
still holds a file. Git marks the old path as one whole-file conflict and treats
the destination as an ordinary added file, so the other side's edits to the old
path merge against a launcher and land nowhere.

Correcting the DETECTION is the fix, not letting a resolver write outside the
conflicted set — `land.sh` grafts the resolution in at conflicted paths only, so
such a write is discarded by design. Stage the destination with the three blobs
the rename would have given it and let the path's own merge driver — the one
`.gitattributes` names, or `git merge-file` — do the port:

    stage 1  the merge base's blob of the OLD path
    mover    the mover's blob of the NEW path
    stranded the stranded side's blob of the OLD path

A clean merge resolves the destination outright; a conflicting one leaves it
genuinely unmerged, so it enters the conflicted set the ordinary way. This
RESOLVES rather than describes, so every doubt refuses before writing anything.
"""

import argparse
import re
import shlex
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _conflict_history import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    run_git,
)
from _conflict_hunks import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    WORKTREE_CONFLICT_STYLE,
    merge_file_style_args,
)
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    bind_repo,
    merge_file_failed,
)
from _merge_attr import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PLAIN_MERGE_ATTRS,
    UNION_MERGE_ATTR,
    MergePolicy,
    decode_attrs,
    effective_driver,
    merge_attrs,
    merge_default,
    policy_of,
)
from _relocation import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Relocation,
    relocations,
)

# A driver the shell could not run at all. Read as "1..127 conflicts" these
# would stage a destination that nothing merged.
_SHELL_CANNOT_RUN = frozenset({126, 127})
_DRIVER_TIMEOUT_SECONDS = 120
# `git config --get` exits 1 for a key that is not set, and non-zero otherwise
# for a lookup that failed. Only the first is git's own text-merge fallback.
_CONFIG_KEY_ABSENT = 1
# git's own default `conflict-marker-size`, used when the path sets none.
_DEFAULT_MARKER_SIZE = 7
# The index stage git calls "ours", which is what a merge driver reads as `%A`.
_OURS_STAGE = ":2"


class PortRefused(Exception):
    """The port did not run, and the merge is exactly as git wrote it."""


@dataclass(frozen=True)
class Ported:
    """What one applied port did, for the caller to report and stage."""

    old_path: str
    destination: str
    # False when `git merge-file` left conflict markers, so the destination is
    # now an ordinary unmerged path for the model to resolve.
    merged_clean: bool


def _blob_bytes(spec: str) -> bytes | None:
    """The raw bytes at a stage or ref, or None when git could not read it.

    Bytes, not text: a port must not be limited to files that decode, and the
    three blobs are written straight back out.
    """
    done = subprocess.run(  # cwd-git-ok: the caller owns its checkout
        ["git", "show", spec], capture_output=True, check=False
    )
    return done.stdout if done.returncode == 0 else None


def _hash_object(data: bytes) -> str:
    done = subprocess.run(  # cwd-git-ok: the caller owns its checkout
        ["git", "hash-object", "-w", "--stdin"],
        input=data,
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        raise PortRefused(
            f"could not write a blob: {done.stderr.decode('utf-8', 'replace')}"
        )
    return done.stdout.decode().strip()


def _index_line(mode: str, sha: str, stage: int, path: str) -> str:
    return f"{mode} {sha} {stage}\t{path}"


def _mode_of(spec: str, path: str) -> str | None:
    """The file mode git records for PATH at SPEC, or None when it has none.

    A stage is spelled with its colon (`:2`) and read from the index; a ref is
    read from its tree.
    """
    if spec.startswith(":"):
        done = run_git("ls-files", "-s", "--", f":(literal){path}")
        stage = spec.lstrip(":")
        for line in done.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[2] == stage:
                return fields[0]
        return None
    done = run_git("ls-tree", spec, "--", f":(literal){path}")
    if done.returncode != 0 or not done.stdout.strip():
        return None
    return done.stdout.split()[0]


def _merged_mode(moved: Relocation) -> str:
    """The destination's mode after the same three-way merge git does on modes.

    The stranded side may have made the old file executable while editing it; a
    real rename merge carries that onto the destination. Taking the mover's mode
    unconditionally would drop the bit and ship a script nothing can run.
    """
    base = _mode_of(":1", moved.path)
    mover = _mode_of(moved.stub_stage, moved.destination) or _mode_of(
        moved.stub_ref, moved.destination
    )
    stranded = _mode_of(moved.stranded_stage, moved.path)
    if mover is None:
        raise PortRefused(f"{moved.path}: {moved.destination} has no recorded mode")
    if stranded is None or stranded == base or stranded == mover:
        return mover
    if mover == base:
        return stranded
    raise PortRefused(
        f"{moved.path}: both sides changed the file mode ({mover} and {stranded}), "
        "which only a human can settle"
    )


@dataclass(frozen=True, kw_only=True, slots=True)
class _MergeVerdict:
    """How this resolver merges one path, as `_merge_attr` reads it."""

    driver: str
    policy: MergePolicy


def _merge_verdicts(paths: list[str]) -> dict[str, _MergeVerdict]:
    """Every PATH's merge verdict, from ONE `check-attr` call over the whole set.

    A call per path is what this costs otherwise: `port_relocations` asks about
    an old path and a destination for every relocation in the merge.

    EFFECTIVE, never raw: `effective_driver` applies the same unbinding the
    resolver writes into `$GIT_DIR/info/attributes`, so a `.yaml` bound to the
    syntax-aware driver answers `text` here as it does in the job that merges.
    """
    default = merge_default()
    return {
        path: _MergeVerdict(
            driver=effective_driver(path, attr, default),
            policy=policy_of(path, attr, default),
        )
        for path, attr in merge_attrs(paths).items()
    }


def _effective_merge_attr(path: str) -> str:
    """The driver this resolver ports PATH with, as `_merge_attr` decides it."""
    return _merge_verdicts([path])[path].driver


def _refuse_unmergeable(path: str, verdict: _MergeVerdict) -> None:
    """Refuse a path the repository said must never be line-merged.

    A `-merge` lockfile silently line-merged into an inconsistent state is the
    case that costs most, and `binary` says the same thing by name.
    """
    if verdict.policy is MergePolicy.UNMERGEABLE:
        raise PortRefused(
            f"{path}: .gitattributes sets `merge={verdict.driver}`, so its merge "
            "is not this pass's to perform"
        )


def _driver_command(attr: str, path: str) -> str | None:
    """The shell command `merge=<attr>` binds, or None for git's own text merge.

    `merge=<name>` with no `merge.<name>.driver` configured is not a refusal:
    git itself falls back to the built-in text merge there, so this does too.
    Only that ABSENT key falls back. An explicitly empty driver is a merge git
    fails, and any other `git config` exit means the lookup itself did not
    answer, so reading either as the text merge merges a path the repository
    would have left conflicted.
    """
    if attr in PLAIN_MERGE_ATTRS:
        return None
    done = run_git("config", "--get", f"merge.{attr}.driver")
    if done.returncode == _CONFIG_KEY_ABSENT:
        return None
    if done.returncode != 0:
        raise PortRefused(
            f"{path}: could not read `merge.{attr}.driver` (git config exited "
            f"{done.returncode}): {done.stderr.strip()}"
        )
    command = done.stdout.strip()
    if not command:
        raise PortRefused(
            f"{path}: `merge.{attr}.driver` is set to an empty command, which is "
            "a merge that fails rather than a text merge"
        )
    return command


def _marker_size(path: str) -> int:
    """PATH's `conflict-marker-size`, which is what git passes a driver as `%L`.

    A repository raises it for a file whose own content holds `<<<<<<<` lines.
    Fabricating 7 hands the driver a size the real merge would not have used.
    """
    done = run_git("check-attr", "-z", "conflict-marker-size", "--", path)
    if done.returncode != 0:
        return _DEFAULT_MARKER_SIZE
    value = decode_attrs(done.stdout).get(path, "")
    return int(value) if value.isdigit() else _DEFAULT_MARKER_SIZE


def _carries_markers(content: bytes, size: int) -> bool:
    """Whether CONTENT holds an opening conflict marker of SIZE characters."""
    opener = b"<" * size
    return any(
        line == opener or line.startswith(opener + b" ")
        for line in content.splitlines()
    )


def _run_driver(command: str, moved: Relocation, scratch: Path) -> tuple[bytes, bool]:
    """Run the repository's own merge driver over the rename's three blobs.

    git's driver contract: `%O` ancestor, `%A` OURS, `%B` THEIRS, and the driver
    leaves its result in `%A` and exits non-zero when conflicts remain. Ours is
    index stage 2 and theirs is stage 3, whichever side did the relocating: a
    driver that keeps one side unconditionally would otherwise keep the wrong
    one and still report the merge clean.

    Every value is shell-quoted, as git's own `ll_ext_merge` quotes each with
    `sq_quote_buf` before expanding it. `%P` is a path the merged branch chose,
    so an unquoted one runs its own shell metacharacters as commands here; the
    scratch paths are absolute, so an unquoted one breaks on a directory name
    that holds a space.
    """
    mover_label = moved.destination
    stranded_label = f"{moved.path} ({moved.stranded_side})"
    if moved.stub_stage == _OURS_STAGE:
        ours, theirs = "mover", "stranded"
        ours_label, theirs_label = mover_label, stranded_label
    else:
        ours, theirs = "stranded", "mover"
        ours_label, theirs_label = stranded_label, mover_label
    values = {
        "%O": shlex.quote(str(scratch / "base")),
        "%A": shlex.quote(str(scratch / ours)),
        "%B": shlex.quote(str(scratch / theirs)),
        "%L": str(_marker_size(moved.destination)),
        "%P": shlex.quote(moved.destination),
        "%S": shlex.quote(f"{moved.path} (merge base)"),
        "%X": shlex.quote(ours_label),
        "%Y": shlex.quote(theirs_label),
    }
    # ONE pass, as git expands its driver line once: a chain of `str.replace`
    # would re-scan a path that itself contains `%S` and expand it as a token.
    filled = re.sub("|".join(values), lambda hit: values[hit.group(0)], command)
    try:
        done = subprocess.run(  # cwd-git-ok: the caller owns its checkout
            filled,
            shell=True,  # noqa: S602  # git runs a merge driver through the shell
            capture_output=True,
            check=False,
            timeout=_DRIVER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as expiry:
        raise PortRefused(
            f"{moved.path}: the `merge` driver did not finish in "
            f"{_DRIVER_TIMEOUT_SECONDS}s merging onto {moved.destination}"
        ) from expiry
    if done.returncode < 0 or done.returncode in _SHELL_CANNOT_RUN:
        raise PortRefused(
            f"{moved.path}: the `merge` driver exited {done.returncode} without "
            f"merging onto {moved.destination}: "
            f"{done.stderr.decode('utf-8', 'replace').strip()}"
        )
    content = (scratch / ours).read_bytes()
    if done.returncode != 0 and not _carries_markers(content, int(values["%L"])):
        # A driver only has to SIGNAL conflicts by its exit status; it does not
        # have to write markers. Staging that result would leave a destination
        # whose worktree text reads resolved, and the next pass then commits one
        # side and drops the other's edits with nothing to see.
        raise PortRefused(
            f"{moved.path}: the `merge` driver reported conflicts merging onto "
            f"{moved.destination} but left no conflict markers, so its result "
            "cannot be staged as an unmerged path"
        )
    return content, done.returncode == 0


def _merge_file(
    moved: Relocation, scratch: Path, *, union: bool = False
) -> tuple[bytes, bool]:
    """git's own three-way text merge of the rename's blobs.

    Argument order IS the orientation: `merge-file current base other` labels
    its markers with the first and third. The mover holds the body, so it is
    `current` and its side's label is the one a reader sees on top.

    `union` asks for git's built-in union driver, which keeps both sides' lines
    instead of writing markers, so `merge=union` gets what it asked for.

    The style is pinned to the one prepare's own merge writes. Every reader of
    the ported file assumes it: mergiraf rebuilds from the base section and
    solves nothing without it, and the model's prompt describes a three-section
    block. `merge-file` writes the plain style unless told otherwise, so the
    style argument is what keeps a ported path shaped like every other
    conflicted file in the tree.
    """
    style = [] if union else merge_file_style_args(WORKTREE_CONFLICT_STYLE)
    merged = subprocess.run(  # cwd-git-ok: the caller owns its checkout
        [
            "git",
            "merge-file",
            "-p",
            *(["--union"] if union else []),
            *style,
            "-L",
            moved.destination,
            "-L",
            f"{moved.path} (merge base)",
            "-L",
            f"{moved.path} ({moved.stranded_side})",
            str(scratch / "mover"),
            str(scratch / "base"),
            str(scratch / "stranded"),
        ],
        capture_output=True,
        check=False,
    )
    if merge_file_failed(merged.returncode):
        raise PortRefused(
            f"{moved.path}: git merge-file exited {merged.returncode} merging the "
            f"stranded edits onto {moved.destination}"
        )
    return merged.stdout, merged.returncode == 0


def _three_way(
    moved: Relocation, scratch: Path, verdict: _MergeVerdict
) -> tuple[bytes, bool]:
    """Merge the rename's three blobs the way THIS repository merges that path.

    The destination's own `merge` attribute decides: a named driver is the merge
    the repository asked for, not a merge it forbade. A destination whose file
    type the syntax-aware driver drops content on is line-merged instead, since
    `_merge_verdicts` reports what the checkout running the merge resolves.
    """
    if verdict.driver == UNION_MERGE_ATTR:
        return _merge_file(moved, scratch, union=True)
    command = _driver_command(verdict.driver, moved.destination)
    if command is None:
        return _merge_file(moved, scratch)
    return _run_driver(command, moved, scratch)


def apply_port(
    moved: Relocation, root: Path, verdicts: dict[str, _MergeVerdict] | None = None
) -> Ported:
    """Stage the destination with the rename's three blobs and merge them.

    VERDICTS is the batched read `port_relocations` already did for the whole
    merge. None asks for this port's own two paths, which is what a caller
    porting ONE relocation wants.

    Raises `PortRefused` before writing anything when any blob is missing, git
    refuses, or the merge is one this pass must not perform itself.
    """
    if verdicts is None:
        bind_repo(root)
        verdicts = _merge_verdicts([moved.path, moved.destination])
    for path in (moved.path, moved.destination):
        _refuse_unmergeable(path, verdicts[path])
    base = _blob_bytes(f":1:{moved.path}")
    mover = _blob_bytes(f"{moved.stub_stage}:{moved.destination}")
    stranded = _blob_bytes(f"{moved.stranded_stage}:{moved.path}")
    if mover is None:
        # The destination is not in the index at all, which is the normal case:
        # only one side added it, so git staged it as an ordinary add.
        mover = _blob_bytes(f"{moved.stub_ref}:{moved.destination}")
    launcher = _blob_bytes(f"{moved.stub_stage}:{moved.path}")
    if base is None or mover is None or stranded is None or launcher is None:
        raise PortRefused(
            f"{moved.path}: the rename's blobs are not all readable, so there is "
            "nothing to merge onto the destination"
        )
    destination_mode = _merged_mode(moved)
    old_mode = _mode_of(moved.stub_stage, moved.path) or "100644"

    # `--absolute-git-dir`, never `root/".git"`: land.sh replays the merge in a
    # LINKED worktree, where `.git` is a FILE holding `gitdir: …`. A mkdir there
    # raises NotADirectoryError, which is not PortRefused, so it would escape the
    # caller's handler and kill the run in the one place the port must work.
    git_dir = run_git("rev-parse", "--absolute-git-dir")
    if git_dir.returncode != 0:
        raise PortRefused(f"{moved.path}: could not locate the git dir for the port")
    scratch = Path(git_dir.stdout.strip()) / "gb-relocation-port"
    scratch.mkdir(parents=True, exist_ok=True)
    for name, data in (("base", base), ("mover", mover), ("stranded", stranded)):
        (scratch / name).write_bytes(data)

    content, clean = _three_way(moved, scratch, verdicts[moved.destination])
    if clean:
        entries = [
            _index_line(destination_mode, _hash_object(content), 0, moved.destination)
        ]
    else:
        # Unmerged: the three stages ARE the conflict, so a later reader — the
        # model's shard, `_out_of_conflict`, the marker verdict — sees the same
        # shape git writes for any other conflict.
        entries = [
            _index_line(destination_mode, _hash_object(base), 1, moved.destination),
            _index_line(
                destination_mode,
                _hash_object(mover),
                int(moved.stub_stage.lstrip(":")),
                moved.destination,
            ),
            _index_line(
                destination_mode,
                _hash_object(stranded),
                int(moved.stranded_stage.lstrip(":")),
                moved.destination,
            ),
        ]
    entries.append(_index_line(old_mode, _hash_object(launcher), 0, moved.path))
    _update_index(moved, entries)
    (root / moved.destination).write_bytes(content)
    (root / moved.path).write_bytes(launcher)
    return Ported(moved.path, moved.destination, clean)


def _update_index(moved: Relocation, entries: list[str]) -> None:
    """Replace the two paths' index entries in one call, or refuse.

    NUL-delimited: a newline is legal in a path, and `_relocation` asks git for
    raw path bytes precisely so such a name survives. A record split on newlines
    would tear that name into a malformed second record.
    """
    records = [f"0 {'0' * 40}\t{path}" for path in (moved.destination, moved.path)]
    records += entries
    payload = "\0".join(records) + "\0"
    done = subprocess.run(  # cwd-git-ok: the caller owns its checkout
        ["git", "update-index", "-z", "--index-info"],
        input=payload.encode(),
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        raise PortRefused(
            f"{moved.path}: could not stage the port: "
            f"{done.stderr.decode('utf-8', 'replace')}"
        )


def port_relocations(root: Path, skip: set[str]) -> list[Ported]:
    """Port every relocation among the paths this merge left unmerged.

    Deterministic in the merge alone — the two parents, the merge base and the
    index — so a caller that re-derives it from its own replay of the same merge
    reaches the same answer without trusting anyone else's.
    """
    unmerged = run_git("diff", "-z", "--name-only", "--diff-filter=U")
    if unmerged.returncode != 0:
        return []
    paths = [name for name in unmerged.stdout.split("\0") if name]
    found = relocations(paths, skip)
    bind_repo(root)
    verdicts = _merge_verdicts(
        sorted(
            {end for moved in found.values() for end in (moved.path, moved.destination)}
        )
    )
    # Two conflicted files consolidated into ONE destination: each port reloads
    # the mover blob, so the second would overwrite the first and drop its
    # stranded edits. Nothing says which mapping is the real one, so refuse both.
    claimed = Counter(moved.destination for moved in found.values())
    done: list[Ported] = []
    for moved in found.values():
        if claimed[moved.destination] > 1:
            print(
                f"::warning::relocation-port: {moved.path} and another conflicted "
                f"path both claim {moved.destination}; porting neither.",
                file=sys.stderr,
            )
            continue
        try:
            done.append(apply_port(moved, root, verdicts))
        except PortRefused as refusal:
            print(f"::warning::relocation-port: {refusal}", file=sys.stderr)
    return done


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="the checkout to act on")
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        help="a path the caller resolves another way; repeatable",
    )
    args = parser.parse_args()
    for ported in port_relocations(Path(args.root).resolve(), set(args.skip)):
        state = "resolved" if ported.merged_clean else "left conflicted"
        print(f"{ported.old_path}\t{ported.destination}\t{state}")


if __name__ == "__main__":
    main()
