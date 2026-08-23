"""Decide whether a merge-resolution hunk (`git show --remerge-diff`) carries anything genuinely new — not a parent's own edit, and not since undone.

PROBLEM CLASS — is a block of diff text still present in some other revision of the file? Counted not searched: a short line (`fi`, `}`) matches anywhere.

Weakening any predicate here fails this instrument OPEN, so four shapes are deliberate: `_line_runs` never joins a run across a conflict marker; `_count_block` counts and never tests membership; `_added_gone_at_head` demands ABSOLUTE absence per line; `hunk_traced_to_the_parents` compares directionally. `.claude/dev-notes` § "Merge-delta novelty judgements (`.github/resolver/_merge_delta_novelty.py`)" carries the reasoning.
"""

import re
from typing import NamedTuple

# A mechanical-merge conflict marker (any of git's four spellings) — never
# valid file content, excluded from every block.
CONFLICT_MARKER = re.compile(r"(?:<{7}|={7}|>{7}|\|{7})")


def _line_runs(hunk: str, sign: str) -> list[str]:
    """The hunk's maximal runs of consecutive `sign`-prefixed lines, joined
    into blocks with the sign stripped. A conflict marker BREAKS a run."""
    lines = hunk.split("\n")[1:]  # [1:] drops the @@ header itself
    runs: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line.startswith(sign) or CONFLICT_MARKER.match(line[1:]):
            if current:
                runs.append("\n".join(current))
                current = []
            continue
        current.append(line[1:])
    if current:
        runs.append("\n".join(current))
    return runs


def _count_block(text: str, block: str) -> int:
    """How many times `block` occurs in `text` as consecutive lines."""
    lines = text.split("\n")
    needle = block.split("\n")
    return sum(
        1
        for i in range(len(lines) - len(needle) + 1)
        if lines[i : i + len(needle)] == needle
    )


def _anchored_runs(hunk: str, sign: str) -> list[str]:
    """{@link _line_runs}, each run prefixed by the line it FOLLOWS in the image
    the sign belongs to — the post-image for `+`, the pre-image for `-`.

    The anchor is what makes the parent comparison POSITIONAL. Counting the run
    alone asks "does this text appear more often in a parent than in the base",
    which is location-agnostic: a parent that added `foo()` at line 500 retires a
    resolution that inserted `foo()` at line 200, so an evil merge clears with no
    human reading it. Anchored, the block a parent must be shown to have added is
    `<the line before it here>` + the run, which that parent added somewhere else
    does not contain.

    The anchored block is checked BESIDE the bare run, never instead of it: an
    anchor can be manufactured by a DELETION. With base `A / OLD / GUARD` and a
    parent that merely dropped `OLD`, `A` + `GUARD` is newly adjacent in that
    parent, so a resolution inserting a second `GUARD` after `A` would clear on
    the anchored count alone. The bare run answers "a parent added this text at
    all", the anchored one "and it did so here"; both must hold.

    A run with no usable neighbour — it opens the hunk, or a conflict marker sits
    where the anchor would be — repeats its bare run as the anchor: the pre-existing comparison,
    and the safe direction, since an unanchored block is HARDER to match.
    """
    lines = hunk.split("\n")[1:]  # [1:] drops the @@ header itself
    # Markers stay IN the image so a run still breaks on one. Filtering them out
    # would splice the two runs a marker separates into one block, which is the
    # weakening this module's header names first: a joined block traces where
    # neither run does, and the hunk retires.
    image = [line for line in lines if line[:1] in (" ", sign)]
    runs: list[str] = []
    current: list[str] = []
    for index, line in enumerate(image):
        if not line.startswith(sign) or CONFLICT_MARKER.match(line[1:]):
            if current:
                runs.append(_anchor_after(image, index - len(current) - 1, current))
                current = []
            continue
        current.append(line[1:])
    if current:
        runs.append(_anchor_after(image, len(image) - len(current) - 1, current))
    return runs


def _anchor_after(image: list[str], at: int, run: list[str]) -> str | None:
    """RUN prefixed by IMAGE's line at AT, or None when that neighbour cannot
    anchor it — the run opens the image, or a conflict marker sits there.

    None, never the bare run: a bare block IS the location-agnostic comparison
    this anchoring exists to replace, so returning one would retire exactly the
    hunks with no context to check against. An un-anchorable run simply never
    traces, and its hunk stays under review.
    """
    if at < 0 or CONFLICT_MARKER.match(image[at][1:]):
        return None
    return "\n".join([image[at][1:], *run])


def hunk_undone_at_head(hunk: str, head_text: str, merge_text: str) -> bool:
    """Is every trace of this hunk's resolution gone at `head` — each added
    block occurring FEWER times than merge, each removed block MORE? `--commit`
    mode (head IS merge) always answers False."""
    added = _line_runs(hunk, "+")
    removed = _line_runs(hunk, "-")
    if not added and not removed:
        return False
    return all(_added_gone_at_head(b, head_text, merge_text) for b in added) and all(
        _count_block(head_text, b) > _count_block(merge_text, b) for b in removed
    )


def _added_gone_at_head(block: str, head_text: str, merge_text: str) -> bool:
    """Does the head carry no trace of this added block — fewer occurrences
    than the merge left, AND every content line absent outright (blanks exempt)?

    The per-line half is load-bearing. `_line_runs` joins consecutive added lines
    into ONE block, so a resolution that adds a comment above a smuggled
    guard-removal makes them a single unit; a later commit that merely REWORDS the
    comment drops the block's count to zero while the smuggled line still ships.

    Absence OUTRIGHT, never "fewer times than the merge left it": the relative test
    is defeatable, since a resolution that adds the smuggled line TWICE and later
    deletes one copy makes the count fall while a copy still ships. The accepted cost
    is that a block holding a line occurring anywhere else at head (`fi`, `}`,
    `done`) can never retire the whole hunk; `corrected_positions` judges each added
    line alone, so that never strands a finding.
    """
    if _count_block(head_text, block) >= _count_block(merge_text, block):
        return False
    return all(
        _line_gone_at_head(line, head_text) or not line.strip()
        for line in block.split("\n")
    )


def _line_gone_at_head(line: str, head_text: str) -> bool:
    """No occurrence of this exact line at head? A blank line always answers False."""
    return bool(line.strip()) and _count_block(head_text, line) == 0


def corrected_positions(hunk: str, head_text: str) -> list[int]:
    """1-based positions, among this hunk's added lines, that the head does not
    carry. POSITIONS not text: quoting PR-controlled text here would grant it
    trusted-input status. `--commit` mode returns none."""
    added = [line[1:] for line in hunk.split("\n") if line.startswith("+")]
    return [i for i, line in enumerate(added, 1) if _line_gone_at_head(line, head_text)]


def relocated_positions(
    hunk: str, merge_text: str, mechanical_text: str, head_text: str
) -> list[int]:
    """1-based positions, among this hunk's removed lines, that this merge kept
    and the head still ships — the resolution MOVED them, it did not delete them.

    Counted against the mechanical text, never tested for presence alone: a guard
    the resolution dropped from one place and left standing in another occurs
    fewer times than the mechanical merge had it, so it stays a removal. The
    `max(1, …)` refuses any line absent from BOTH texts, which a bare count
    comparison would satisfy at zero.

    The HEAD conjunct is what stops this failing open. Judged against the merge
    alone, a resolution that moved a guard and a later commit that deleted where
    it moved TO leave the reviewer told to raise no deletion finding on a line
    shipping nowhere — the `+` half retires as undone, the `-` half stays in the
    fence, and nothing names the loss. Presence is the exact question there, so
    this one conjunct is membership: one surviving occurrence is a line the merge
    did not delete, whatever a later commit did to a second copy. In `--commit`
    mode the head IS the merge, so the conjunct is free.

    POSITIONS not text, for the reason `corrected_positions` carries. A blank
    line and a conflict marker are never named: neither is content a reviewer can
    judge, and a merged file may legitimately hold a line of `=` signs.
    """
    removed = [line[1:] for line in hunk.split("\n") if line.startswith("-")]
    return [
        i
        for i, line in enumerate(removed, 1)
        if line.strip()
        and not CONFLICT_MARKER.match(line)
        and _count_block(merge_text, line)
        >= max(1, _count_block(mechanical_text, line))
        and _count_block(head_text, line) >= 1
    ]


class ParentBlobs(NamedTuple):
    """One merge's three reference texts: common ancestor, and each parent."""

    base: str
    parent1: str
    parent2: str


def _one_parent_edited(
    blobs: ParentBlobs, bare: str, anchored: str | None, *, added: bool
) -> bool:
    """Did ONE parent make this edit, both as text and at this place?

    One parent, not "some parent for each half": splitting the two questions
    across the two parents retires a block neither of them wrote there. With base
    `A / OLD / GUARD`, parent 1 appending a second `GUARD` and parent 2 merely
    dropping `OLD`, parent 1 answers the text half and parent 2 the position
    half, so a `GUARD` inserted after `A` clears on nobody's authority.

    An unanchored run answers False: see {@link _anchor_after}.
    """
    if anchored is None:
        return False
    for parent in (blobs.parent1, blobs.parent2):
        counts = [
            (_count_block(parent, block), _count_block(blobs.base, block))
            for block in (bare, anchored)
        ]
        if all(
            (mine > theirs) if added else (theirs > mine) for mine, theirs in counts
        ):
            return True
    return False


def hunk_traced_to_the_parents(hunk: str, blobs: ParentBlobs) -> bool:
    """Is every block this hunk touches already one parent's edit against the
    merge-base, AT THIS PLACE (removed = fewer at base, added = more)?
    Directional, so a parent-added guard the resolution deleted stays under
    review; anchored, so a parent that added the same line SOMEWHERE ELSE does
    not excuse an insertion nobody made here (see {@link _anchored_runs}).

    A WHOLE run must match, which refuses the ordinary two-sided resolution and
    sends it to the reviewer. Splitting a run into segments that each trace is
    the tempting repair and it fails OPEN, per line and per segment alike: for
    parent 1 adding `if allowed:` / `run()` and parent 2 adding `if not allowed:`
    / `deny()`, a resolution writing `if not allowed:` / `run()` has every line,
    and both one-line segments in their parent's own order, tracing to a parent.
    A safe predicate has to demand each segment be a parent's COMPLETE inserted
    block against the merge-base, never an arbitrary slice of one.
    """
    return all(
        _one_parent_edited(blobs, bare, anchored, added=False)
        for bare, anchored in zip(_line_runs(hunk, "-"), _anchored_runs(hunk, "-"))
    ) and all(
        _one_parent_edited(blobs, bare, anchored, added=True)
        for bare, anchored in zip(_line_runs(hunk, "+"), _anchored_runs(hunk, "+"))
    )
