"""Decide whether a merge-resolution hunk (`git show --remerge-diff`) carries anything genuinely new — not a parent's own edit, and not since undone.

PROBLEM CLASS — is a block of diff text still present in some other revision of the file? Counted not searched: a short line (`fi`, `}`) matches anywhere.

Weakening any predicate here fails this instrument OPEN, so each of these shapes is deliberate: `_line_runs` never joins a run across a conflict marker; `_count_block` counts and never tests membership; `_added_gone_at_head` demands ABSOLUTE absence per line; `hunk_traced_to_the_parents` compares directionally, and asks for an ANCHOR wherever the resolution chose the position — everywhere git did not hand it one; `forced_collisions` names a NAME and retires nothing, because the removed lines of a de-duplication carry no tie to the definition they came from; `hunk_strips_trailing_whitespace` retires the stripping direction only; `blocks_carried_at_head` counts whole BLOCKS, because it is the one predicate here whose true answer stands a reviewer down. `.claude/dev-notes` § "Merge-delta novelty judgements (`.github/resolver/_merge_delta_novelty.py`)" carries the reasoning.
"""

import ast
import re
from collections import Counter
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "auto-resolve"))
# pylint: disable=wrong-import-position  # must follow the sys.path insert above
from _conflict_hunks import closes_conflict, opens_conflict  # noqa: E402,I001

# A mechanical-merge conflict marker (any of git's four spellings) — never valid
# file content, excluded from every block. Looser than `_conflict_hunks`'
# `_MARKER_RE`, which demands a space or the line end after the seven characters.
# Under that one an eight-character `========` stops breaking a run, the two runs
# it separates join into a block that traces, and this instrument fails open.
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


def blocks_carried_at_head(hunk: str, sign: str, head_text: str) -> tuple[int, int]:
    """How many of this hunk's `sign` blocks the head still holds, and how many
    there are.

    BLOCKS, never lines, and that is the whole safety of it. Presence is the
    CLAIM here, where `corrected_positions` asks the absent direction and a
    false answer merely keeps a hunk under review. A short line (`fi`, `}`)
    occurs somewhere in almost any file, so a per-line test would report content
    as carried that the head does not hold as this block — the fail-open
    direction this module's header names, wired into a note that tells the
    reviewer to stand down.

    A blank-only run is dropped, for the reason blanks are exempt throughout: it
    carries nothing a reviewer can judge.
    """
    runs = [run for run in _line_runs(hunk, sign) if run.strip()]
    # Counted as a MULTISET: two identical blocks need two occurrences at head.
    # Asking each one separately lets a single survivor answer for both, and the
    # note then tells the reviewer that all of a resolution ships when half does.
    available = Counter(runs)
    for run in available:
        available[run] = min(available[run], _count_block(head_text, run))
    return sum(available.values()), len(runs)


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


def _anchor_kept_its_place(holder: str, other: str, anchor: str) -> bool:
    """Does ANCHOR sit at the same place in both texts?

    The line BEFORE it cannot answer this. A parent that moves the anchor and
    its predecessor TOGETHER keeps that predecessor and lands its edit at
    another site, so the hunk retires on an addition made somewhere else: with
    base `P / A / X / Y` and parent `X / Y / P / A / GUARD`, `P` precedes `A` in
    both.

    ORDER answers it. Every line that kept its side of the anchor left the
    anchor where it was, and a line that crossed from one side to the other
    moved it — `X` and `Y` follow the anchor in the base and precede it in that
    parent. Only a line occurring ONCE in each text is read, since a repeated
    line names no single position.

    An anchor absent from OTHER came with the edit and has no place to compare,
    which the caller already handles.
    """
    if _count_block(other, anchor) == 0:
        return True
    holder_lines = holder.split("\n")
    other_lines = other.split("\n")
    if anchor not in holder_lines or anchor not in other_lines:
        return False
    here, there = holder_lines.index(anchor), other_lines.index(anchor)

    def spots(lines: list[str]) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for index, line in enumerate(lines):
            out.setdefault(line, []).append(index)
        return out

    mine = spots(holder_lines)
    for line, theirs in spots(other_lines).items():
        ours = mine.get(line)
        if line == anchor or ours is None or len(ours) != 1 or len(theirs) != 1:
            continue
        if (ours[0] < here) != (theirs[0] < there):
            return False
    return True


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
    # the parent made somewhere else.
    if not _anchor_kept_its_place(holder, other, anchor_line):
        return False
    # An anchor the base never held came with this edit — unless the SIBLING
    # introduced one too, and then two parents put the same anchor at two sites
    # and the hunk names neither.
    return (
        _count_block(other, anchor_line) == 1 or _count_block(sibling, anchor_line) == 0
    )


def _runs_inside_a_conflict(hunk: str, sign: str) -> list[bool]:
    """For each run `_line_runs` yields, whether it sits INSIDE a conflict
    region — between git's `<<<<<<<` and `>>>>>>>` in the image its sign
    belongs to.

    The two halves of the file ask different questions. Inside a conflict git
    chose the position, not the resolution, and every run there is
    marker-adjacent, so an anchor is unavailable and asking for one refuses
    every conflicted hunk. Outside one the resolution chose the position, and
    the anchor is the only thing that separates an edit made HERE from the same
    text a parent added elsewhere.
    """
    # ADDED lines are the resolution's own bytes, so a marker among them is
    # PR-controlled text. Trusting it would let a resolution write `<<<<<<<`
    # above its invented lines and buy them the weaker test. Only the pre-image
    # carries markers git wrote.
    if sign != "-":
        return [False] * len(_line_runs(hunk, sign))
    out: list[bool] = []
    depth = 0
    run_open = False
    for line in hunk.split("\n")[1:]:  # [1:] drops the @@ header itself
        if line[:1] not in (" ", sign):
            continue
        text = line[1:]
        if CONFLICT_MARKER.match(text):
            run_open = False
            if opens_conflict(text):
                depth += 1
            elif closes_conflict(text):
                depth = max(0, depth - 1)
            continue
        if not line.startswith(sign):
            run_open = False
            continue
        if not run_open:
            out.append(depth > 0)
            run_open = True
    return out


def _one_parent_counted(blobs: ParentBlobs, block: str, *, added: bool) -> bool:
    """Did ONE parent make this edit at all, position aside?

    The question for a run git itself delimited. The resolution picked which
    side to keep, never where it went, so there is no placement to check — and
    the direction still holds: a block one parent ADDED that the resolution
    DELETED has a base count of zero and never passes.
    """
    if added:
        return max(
            _count_block(blobs.parent1, block), _count_block(blobs.parent2, block)
        ) > _count_block(blobs.base, block)
    return _count_block(blobs.base, block) > min(
        _count_block(blobs.parent1, block), _count_block(blobs.parent2, block)
    )


def hunk_traced_to_the_parents(hunk: str, blobs: ParentBlobs) -> bool:
    """Is every block this hunk touches one parent's own edit against the
    merge-base — each removed block deleted by that parent, each added block
    added by it, and OUTSIDE a conflict region at this place too?

    The comparison is directional, and that direction is the safety argument: a
    guard one parent ADDED and the resolution DELETED has a base count of zero,
    so the count test fails and the hunk stays under review.
    """
    for sign, added in (("-", False), ("+", True)):
        bare = _line_runs(hunk, sign)
        anchored = _anchored_runs(hunk, sign)
        inside_a_conflict = _runs_inside_a_conflict(hunk, sign)
        # strict=: the three lists are one entry per run by construction, so a
        # length that ever diverges is a run judged by another run's evidence.
        for block, anchor, inside in zip(
            bare, anchored, inside_a_conflict, strict=True
        ):
            if inside:
                if not _one_parent_counted(blobs, block, added=added):
                    return False
            elif not _one_parent_edited(blobs, block, anchor, added=added):
                return False
    return True


# Exactly what `git diff --check` reports as a trailing blank. A vertical tab
# or form feed is content to git, so stripping one is a delta to review.
_TRAILING_WS = " \t\r"


def _replacement_blocks(hunk: str) -> list[tuple[list[str], list[str]]]:
    """This hunk's REMOVE-then-ADD blocks, each a run of `-` lines immediately
    followed by a run of `+` lines with no context line between them.

    A context line between a hunk's removed and added halves means the
    resolution MOVED a line past it rather than edited it in place, so pairing
    `removed[i]` with `added[i]` across that gap compares two lines that were
    never the same position. Splitting on every context line is what keeps each
    pair inside one genuine replacement.
    """
    lines = hunk.split("\n")[1:]  # [1:] drops the @@ header itself
    blocks: list[tuple[list[str], list[str]]] = []
    removed: list[str] = []
    added: list[str] = []
    for line in lines:
        if line.startswith("-"):
            if added:
                blocks.append((removed, added))
                removed, added = [], []
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
        elif removed or added:
            blocks.append((removed, added))
            removed, added = [], []
    if removed or added:
        blocks.append((removed, added))
    return blocks


def hunk_strips_trailing_whitespace(hunk: str) -> bool:
    """Whether this hunk only removes trailing whitespace, line for line.

    A commit-time whitespace guard — `git diff --check`, pre-commit's
    `trailing-whitespace` — refuses to let a commit carry those bytes. The delta
    reviewer would otherwise read the removal as content present in neither
    parent, and ask for bytes only a hook bypass can restore. Which paths such a
    guard covers is a question about the caller's repository, which
    `remerge-diff-report.strips_are_mandated_for` asks git.

    Only the stripping direction retires. A hunk that ADDS trailing whitespace
    is not something the guards force, and stays under review. A bare `rstrip()`
    would drop a trailing non-breaking space, vertical tab or form feed, none of
    which any guard forces gone.
    """
    blocks = _replacement_blocks(hunk)
    if not blocks:
        return False
    return all(
        removed
        and len(removed) == len(added)
        and all(
            rem != add
            and rem.rstrip(_TRAILING_WS) == add.rstrip(_TRAILING_WS)
            and add == add.rstrip(_TRAILING_WS)
            for rem, add in zip(removed, added, strict=True)
        )
        for removed, added in blocks
    )


def _top_level_definitions(text: str) -> dict[str, list[str]] | None:
    """NAME -> the source of each top-level `def`/`class` binding it, or None
    when TEXT is not parseable Python.

    Decorators are part of the definition: a run of removed lines starts at the
    `@`, so a segment that began at the `def` would never contain it.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    lines = text.split("\n")
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if (
            not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            or node.end_lineno is None
        ):
            continue
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        out.setdefault(node.name, []).append(
            "\n".join(lines[start - 1 : node.end_lineno])
        )
    return out


def forced_collisions(merged_text: str, blobs: ParentBlobs) -> list[str]:
    """The top-level NAMES both parents added that this merge could only keep
    once, because Python binds the last `def` or `class` of a name.

    NAMES, never line positions, and that is the safety argument: git shares the
    two copies' identical `def` line as CONTEXT and marks only the bodies, so a
    de-duplication's removed lines can be one common body line that ties to
    nothing. So this retires no line; it says why one copy is gone.

    Every ambiguity refuses:

    - the merge-base must NOT bind the name, or both parents merely EDITED it
      and the dropped copy is an ordinary choice that may have lost behaviour;
    - the merged file must bind it once, and each parent once;
    - the survivor must be one parent's own bytes. Parents that added the SAME
      definition are the least ambiguous case, not an excluded one.

    A name ships unescaped because `ast` produces it: a Python identifier holds
    no backtick and no newline. Python only; another language answers empty.
    """
    merged = _top_level_definitions(merged_text)
    base = _top_level_definitions(blobs.base)
    ours = _top_level_definitions(blobs.parent1)
    theirs = _top_level_definitions(blobs.parent2)
    if merged is None or base is None or ours is None or theirs is None:
        return []
    return sorted(
        name
        for name, kept in merged.items()
        if name not in base
        and len(kept) == 1
        and len(ours.get(name, [])) == 1
        and len(theirs.get(name, [])) == 1
        and kept[0] in (ours[name][0], theirs[name][0])
    )
