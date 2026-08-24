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
    its sign belongs to — the post-image for `+`, the pre-image for `-`.

    The anchor makes the parent comparison POSITIONAL. Counting the run alone
    asks whether the text appears more often in a parent than in the base, which
    is location-agnostic: a parent that added `foo()` at line 500 retires a
    resolution that inserted `foo()` at line 200, and that evil merge clears
    with no human reading it.

    Markers stay in the image so a run still BREAKS on one. Filtering them out
    splices the two runs a marker separates into one block, which is the
    weakening this instrument's header names first.
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
    the anchor replaces, so falling back to one retires exactly the runs with no
    context to check. An un-anchorable run never traces.
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
    """Did ONE parent make this edit, at a site both texts identify UNIQUELY?

    Counting alone cannot answer "at this place", and three separate leaks
    proved it: a parent that added the text elsewhere, a parent that merely
    deleted the line before it, and a parent whose two unrelated edits each
    satisfied one half. Each was a way for two count increases to come from two
    different sites.

    So the site must be unambiguous, and refusal is the answer when it is not.
    The run and its anchored form must each occur EXACTLY once in the parent and
    at most once in the base: one occurrence is one site, and the two counts then
    have nowhere else to come from. `A / X / A / Y` refuses because the anchor
    `A` is ambiguous; a parent holding two `GUARD` refuses because the run is.

    An un-anchored run answers False; see {@link _anchor_after}.
    """
    if anchored is None:
        return False
    return any(
        _edited_uniquely(parent, sibling, blobs.base, bare, anchored, added=added)
        for parent, sibling in (
            (blobs.parent1, blobs.parent2),
            (blobs.parent2, blobs.parent1),
        )
    )


def _same_predecessor(holder: str, other: str, anchor: str) -> bool:
    """Does ANCHOR follow the same line in both texts?

    Only asked when the anchor occurs in both, so a move shows up as a changed
    neighbour. An anchor absent from OTHER came with the edit and has no
    predecessor to compare, which the caller already handles.
    """
    if _count_block(other, anchor) == 0:
        return True

    def before(text: str) -> str | None:
        lines = text.split("\n")
        index = lines.index(anchor) if anchor in lines else -1
        if index < 0:
            return None
        return lines[index - 1] if index else ""

    return before(holder) == before(other)


def _edited_uniquely(
    parent: str, sibling: str, base: str, bare: str, anchored: str, *, added: bool
) -> bool:
    """One parent's answer for {@link _one_parent_edited}, refusing ambiguity."""
    # The side that must hold the edit: the parent for an addition, the base for
    # a deletion. Exactly one occurrence there, so the site is a single place.
    holder, other = (parent, base) if added else (base, parent)
    for block in (bare, anchored):
        if _count_block(holder, block) != 1 or _count_block(other, block) != 0:
            return False
    # The ANCHOR LINE too, on BOTH sides, and this is what the block counts miss:
    # with base `A / X / A / Y` a parent can edit after the SECOND `A` while the
    # resolution edits after the FIRST, and both blocks stay unique. Zero on the
    # other side is fine — the parent brought the anchor with the run, so no
    # earlier occurrence competes with it.
    anchor_line = anchored.split("\n")[0]
    if anchor_line == bare:
        return True
    if _count_block(holder, anchor_line) != 1 or _count_block(other, anchor_line) > 1:
        return False
    # The anchor must also still be in the SAME PLACE. Counts cannot tell an
    # anchor that stayed and gained a line from one the parent MOVED and gained
    # a line at its new home: with base `A / X / Y` and parent `X / Y / A /
    # GUARD`, every count above is 1, and retiring the hunk clears an insertion
    # the parent made somewhere else. The line before it answers that cheaply.
    if not _same_predecessor(holder, other, anchor_line):
        return False
    # An anchor the base never held came with this edit — unless the SIBLING
    # introduced one too, and then two parents put the same anchor at two sites
    # and the hunk names neither.
    return (
        _count_block(other, anchor_line) == 1 or _count_block(sibling, anchor_line) == 0
    )


def hunk_traced_to_the_parents(hunk: str, blobs: ParentBlobs) -> bool:
    """Is every block this hunk touches one parent's own edit against the
    merge-base, AT THIS PLACE — each removed block deleted by that parent, each
    added block added by it?

    This is the question the reviewer is asked — "does one side's intent explain
    this hunk?" — answered from the three blobs rather than from a list of
    commit subjects, which is all the reviewer gets. Answering it here is what
    lets an ordinary resolution clear with no human reading it.

    The comparison is directional, and that direction is the safety argument: a
    guard one parent ADDED and the resolution DELETED has a base count of zero,
    so `0 > 0` fails and the hunk stays under review.

    Blocks are attributed independently — a hunk may follow one side's deletion
    and the other's addition. A hunk whose every signed line is a conflict
    marker yields no blocks and passes vacuously, which is correct: a marker is
    never valid file content.
    """
    return all(
        _one_parent_edited(blobs, bare, anchored, added=False)
        for bare, anchored in zip(_line_runs(hunk, "-"), _anchored_runs(hunk, "-"))
    ) and all(
        _one_parent_edited(blobs, bare, anchored, added=True)
        for bare, anchored in zip(_line_runs(hunk, "+"), _anchored_runs(hunk, "+"))
    )
