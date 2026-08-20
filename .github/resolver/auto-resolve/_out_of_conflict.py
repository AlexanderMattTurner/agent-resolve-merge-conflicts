"""PROBLEM CLASS — a merge resolution rewrote lines both parents left identical.

A shard resolving one conflict hunk can also touch text nothing put in
conflict, because nothing stops it from rewriting a line outside the hunk it
was asked to resolve. On agent-glovebox PR #4492 a resolution re-indented a
12-line comment (2 spaces to 8) on lines byte-identical between both parents,
and nothing that only checks the hunks themselves catches an edit outside them.

This diffs the mechanical merge blob (markers included) against the resolved
text and flags every changed line range that falls entirely outside every
conflict span `_conflict_hunks.segments` found. A pure insertion right at a
span boundary is not flagged: `difflib` can anchor it on either side of the
span, so a resolution that legitimately deletes a span and appends replacement
text must not be misread as an out-of-span edit.
"""

import difflib
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _conflict_hunks import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Hunk,
    segments,
)


@dataclass(frozen=True)
class Violation:
    """One changed range difflib found on the mechanical side wholly outside
    every conflict span. `mech_start > mech_end` marks a pure insertion: the
    mechanical side contributed no lines, so its range is empty by convention
    (`mech_start == mech_end + 1`)."""

    mech_start: int
    mech_end: int
    res_start: int
    res_end: int


def conflict_spans(mechanical_text: str) -> list[tuple[int, int]] | None:
    """1-based inclusive line ranges of each conflict hunk in MECHANICAL_TEXT,
    marker lines included. None when `segments` refuses (malformed markers) or
    the text has no hunks at all — both mean there is no in-span/out-of-span
    line to distinguish, so the caller must skip the file."""
    parts = segments(mechanical_text)
    if parts is None:
        return None
    spans = []
    line = 1
    for part in parts:
        text = part.text if isinstance(part, Hunk) else part
        length = len(text.splitlines())
        if isinstance(part, Hunk):
            spans.append((line, line + length - 1))
        line += length
    return spans if spans else None


def _in_span_with_slop(spans: list[tuple[int, int]], i1: int) -> bool:
    # difflib's 0-based `i1` for an insert already reads as a 1-based "insert
    # after line i1" position (i1 lines of the mechanical side precede it), so
    # no further conversion is needed here.
    return any(start - 1 <= i1 <= end for start, end in spans)


def _overlaps_span(spans: list[tuple[int, int]], i1: int, i2: int) -> bool:
    # i1/i2 are difflib's 0-based, half-open opcode indices for a non-empty
    # mechanical range; +1 makes the lower bound 1-based inclusive.
    return any(start <= i2 and i1 + 1 <= end for start, end in spans)


def out_of_conflict_hunks(mechanical_text: str, resolved_text: str) -> list[Violation]:
    """Every changed range RESOLVED_TEXT introduces outside a conflict span in
    MECHANICAL_TEXT. [] when `conflict_spans` finds nothing to gate. Missing a
    real out-of-span edit is worse than refusing a correct resolution, so a
    `replace` or `delete` opcode needs no slop: it is a violation the moment it
    fails to overlap a span. Only a pure `insert` gets the boundary allowance."""
    spans = conflict_spans(mechanical_text)
    if spans is None:
        return []
    mech_lines = mechanical_text.splitlines(keepends=True)
    res_lines = resolved_text.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, mech_lines, res_lines, autojunk=False)
    violations = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            if _in_span_with_slop(spans, i1):
                continue
        elif _overlaps_span(spans, i1, i2):
            continue
        mech_start, mech_end = (i1 + 1, i2) if i1 < i2 else (i1 + 1, i1)
        violations.append(Violation(mech_start, mech_end, j1 + 1, j2))
    return violations
