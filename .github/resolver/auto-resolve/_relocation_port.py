"""Give a missed rename the three-way merge git would have done.

PROBLEM CLASS — git failed to detect a rename, so one side's edits have no
merge to take part in. When a side moves a file's body to a new path and leaves
a launcher at the old one, rename detection cannot fire, because the old path
still holds a file. Git then marks the old path as one whole-file conflict and
treats the destination as an ordinary added file — so the OTHER side's edits to
the old path merge against a launcher that no longer contains the code they
edit, and they land nowhere.

The fix is not to let a resolver write outside the conflicted set: `land.sh`
re-derives that set from its own replay and grafts the resolution in at those
paths only, so such a write is discarded by design. The fix is to correct the
DETECTION — stage the destination with the three blobs the rename would have
given it, and let `git merge-file` do the port:

    stage 1  the merge base's blob of the OLD path (the moved content's ancestor)
    mover    the mover's blob of the NEW path      (the body, where it lives now)
    stranded the stranded side's blob of the OLD path (the edits with no home)

A clean merge resolves the destination outright. A conflicting one leaves the
destination genuinely unmerged, so it enters the conflicted set the ordinary way
and every existing guard applies to it unchanged.

Refusals restore the index, because a port that half-applied is worse than one
that never ran: the caller's merge must be byte-identical after a refusal.
"""

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _conflict_history import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    run_git,
)
from _relocation import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Relocation,
    relocations,
)

# git's own exit codes for `merge-file`: 0 clean, 1..127 that many conflicts,
# and anything above an error. A negative value is a signal.
_MERGE_FILE_MAX_CONFLICTS = 127
# The stage numbers, in the merge's own orientation. `_relocation` labels the
# sides in words; these map a label back to the stage git reads.
_STAGE_OF_SIDE = {"this PR": 2, "the base branch": 3}


class PortRefused(Exception):
    """The port could not be applied, and the index is back as it was."""


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


def _index_line(mode: str, sha: str, stage: int, path: str) -> str:
    return f"{mode} {sha} {stage}\t{path}"


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


def _mode_of(spec: str, path: str) -> str:
    """The file mode git records for PATH at SPEC, defaulting to a plain file.

    The destination keeps the mode the mover gave it; an executable launcher
    that lost its bit would break the boot that execs it.
    """
    done = run_git("ls-tree", spec, "--", f":(literal){path}")
    if done.returncode != 0 or not done.stdout.strip():
        return "100644"
    return done.stdout.split()[0]


def apply_port(moved: Relocation, root: Path) -> Ported:
    """Stage the destination with the rename's three blobs and merge them.

    Raises `PortRefused` when any blob is missing or git refuses, having left
    the index untouched — the caller's merge is then exactly as it was.
    """
    mover_stage = _STAGE_OF_SIDE[moved.stub_side]
    stranded_stage = _STAGE_OF_SIDE[moved.stranded_side]
    base = _blob_bytes(f":1:{moved.path}")
    mover = _blob_bytes(f":{mover_stage}:{moved.destination}")
    stranded = _blob_bytes(f":{stranded_stage}:{moved.path}")
    if mover is None:
        # The destination is not in the index at all, which is the normal case:
        # only one side added it, so git staged it as an ordinary add.
        mover_ref = "HEAD" if mover_stage == 2 else "MERGE_HEAD"
        mover = _blob_bytes(f"{mover_ref}:{moved.destination}")
    if base is None or mover is None or stranded is None:
        raise PortRefused(
            f"{moved.path}: the rename's three blobs are not all readable, so "
            "there is nothing to merge onto the destination"
        )

    scratch = root / ".git" / "gb-relocation-port"
    scratch.mkdir(parents=True, exist_ok=True)
    names = {"base": base, "mover": mover, "stranded": stranded}
    for name, data in names.items():
        (scratch / name).write_bytes(data)
    # Argument order IS the orientation: `merge-file current base other` labels
    # its markers with the first and third. The mover holds the body, so it is
    # `current` and its side's label is the one a reader sees on top.
    merged = subprocess.run(  # cwd-git-ok: the caller owns its checkout
        [
            "git",
            "merge-file",
            "-p",
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
    if merged.returncode < 0 or merged.returncode > _MERGE_FILE_MAX_CONFLICTS:
        raise PortRefused(
            f"{moved.path}: git merge-file exited {merged.returncode} merging the "
            f"stranded edits onto {moved.destination}"
        )
    clean = merged.returncode == 0

    destination_mode = _mode_of(
        "HEAD" if mover_stage == 2 else "MERGE_HEAD", moved.destination
    )
    old_mode = _mode_of("HEAD" if mover_stage == 2 else "MERGE_HEAD", moved.path)
    launcher = _blob_bytes(f":{mover_stage}:{moved.path}")
    if launcher is None:
        raise PortRefused(f"{moved.path}: the launcher blob is not in the index")

    entries = [
        _index_line(destination_mode, _hash_object(merged.stdout), 0, moved.destination)
        if clean
        else "",
        _index_line(old_mode, _hash_object(launcher), 0, moved.path),
    ]
    if not clean:
        # Unmerged: the three stages ARE the conflict, so a later reader — the
        # model's shard, `_out_of_conflict`, the marker verdict — sees the same
        # shape git writes for any other conflict.
        entries[0] = "\n".join(
            (
                _index_line(destination_mode, _hash_object(base), 1, moved.destination),
                _index_line(
                    destination_mode,
                    _hash_object(mover),
                    mover_stage,
                    moved.destination,
                ),
                _index_line(
                    destination_mode,
                    _hash_object(stranded),
                    stranded_stage,
                    moved.destination,
                ),
            )
        )
    _update_index(moved, entries)
    (root / moved.destination).write_bytes(merged.stdout)
    (root / moved.path).write_bytes(launcher)
    return Ported(moved.path, moved.destination, clean)


def _update_index(moved: Relocation, entries: list[str]) -> None:
    """Replace the two paths' index entries in one call, or refuse."""
    removals = "\n".join(
        f"0 {'0' * 40}\t{path}" for path in (moved.destination, moved.path)
    )
    payload = removals + "\n" + "\n".join(e for e in entries if e) + "\n"
    done = subprocess.run(  # cwd-git-ok: the caller owns its checkout
        ["git", "update-index", "--index-info"],
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
    done: list[Ported] = []
    for moved in relocations(paths, skip).values():
        try:
            done.append(apply_port(moved, root))
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
