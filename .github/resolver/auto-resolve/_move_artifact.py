"""The two whole parent files a MOVE-ARTIFACT conflict block is resolved from.

PROBLEM CLASS — a conflict block that holds no answer at all. Git cuts a
conflict region by aligning lines, so when both sides MOVE a run of definitions
in opposite directions it can align one side's text against different text on
the other side. `_conflict_hunks.is_move_artifact` reads that shape. The block
then says nothing about which definitions belong in the region, and the answer
is in the two parent files instead. A shard cannot run git, so this module
writes both parents into the run's scratch directory and the shard reads them.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _conflict_history import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    run_git,
)
from _conflict_hunks import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Hunk,
    is_move_artifact,
)

# The two merge parents, as git names them in the mid-merge tree this runs in:
# HEAD is the pull request's side, MERGE_HEAD the base branch's.
OURS_REF = "HEAD"
THEIRS_REF = "MERGE_HEAD"


@dataclass(frozen=True)
class MoveParents:
    """Where this run wrote the two whole parent files, for a shard that may
    READ both and write neither."""

    ours: str
    theirs: str


def _parent_text(ref: str, file: str) -> str:
    """FILE as REF holds it, or "" when this run cannot read it as text there.

    "" disables the check for that block, which is what every block did before
    this check existed. A parent REF simply has no version of is ordinary and
    silent. A git that did not RUN, and a blob that is not UTF-8 text, are said
    out loud: each disables the check with no other signal.
    """
    try:
        done = run_git("show", f"{ref}:{file}", text=False)
    except OSError as failure:
        print(
            f"::warning::could not read {ref}:{file} ({failure}), so this run "
            "cannot tell whether either side MOVED a block of it."
        )
        return ""
    if done.returncode != 0:
        return ""
    try:
        return done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        print(
            f"::warning::{ref}:{file} is not UTF-8 text, so this run cannot "
            "tell whether either side MOVED a block of it."
        )
        return ""


def _write_parents(scratch: Path, file: str, texts: dict[str, str]) -> MoveParents:
    """TEXTS written under SCRATCH, each under a directory named for its ref and
    keeping FILE's own name — the two paths a shard's prompt points at."""
    written = {}
    for ref, text in texts.items():
        path = scratch / "parents" / ref / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written[ref] = str(path)
    return MoveParents(written[OURS_REF], written[THEIRS_REF])


def flag_move_artifacts(
    file: str, blocks: list[Hunk], scratch: Path
) -> tuple[set[int], MoveParents | None]:
    """Which of BLOCKS are move artifacts, and where their two parent files went.

    The ordinals are empty for almost every file, and nothing is written then.
    The parents are None when this run could not write them: the shard still
    reads a block holding no answer, and `_marker_verdict` declines rather than
    handing off a timeout no further clock can answer.
    """
    if not blocks:
        return set(), None
    texts = {ref: _parent_text(ref, file) for ref in (OURS_REF, THEIRS_REF)}
    moved = {
        block.ordinal
        for block in blocks
        if is_move_artifact(block.text, texts[OURS_REF], texts[THEIRS_REF])
    }
    if not moved:
        return set(), None
    try:
        return moved, _write_parents(scratch, file, texts)
    except OSError as failure:
        print(
            f"::warning::could not write the merge parents of {file} "
            f"({failure}), so its shard resolves a block that holds no answer."
        )
        return moved, None


def parent_grant(parents: MoveParents | None) -> str:
    """The paths this shard may READ outside the merged tree, one per line —
    the spelling `shard-permission.mjs` splits. Empty for every shard but one
    that owns a move artifact, so a run that needs no parent file grants none.
    """
    return "" if parents is None else f"{parents.ours}\n{parents.theirs}"
