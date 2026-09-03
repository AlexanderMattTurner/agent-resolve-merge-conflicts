"""Re-cutting a JSON array's conflicts entry by entry instead of line by line.

PROBLEM CLASS — a shard's whole assignment is one conflict block, so a block
that is a rewritten LIST is a block no shard finishes inside
`SHARD_TIMEOUT_SECONDS`. agent-glovebox#5644 records five runs that each handed
the same `.github/sbx-live/checks.json` back untouched: the base branch had
added a field to all 41 entries and the head had retired 21 of them, so git
wrote the list as two blocks of 40 and 15 lines that both sides rewrote. The
rule a person then applied was per ENTRY, and it only reads that way over a
parsed list.

Git cuts a file on its own LINE diff, which knows nothing about the sibling
objects of a JSON array — it aligns on the `},` between two entries, so a block
routinely starts in the middle of one entry and ends in the middle of another.
This re-cuts the same merge on the array's element boundaries: an entry both
sides wrote identically leaves the conflict altogether, and every entry that
differs becomes a block of its own. The shard then holds one entry's versions.

The cut is taken over the whole file rather than one block at a time, because a
block is a fragment: its first and last entries are split across the marker, and
neither half parses. Reading each side of the file back gives whole entries.

INVARIANT — the re-cut file resolves to the SAME bytes as the original under
"take ours everywhere" and under "take theirs everywhere". :func:`narrow` checks
that on every file it would rewrite and keeps the original text when it does not
hold, so a misalignment costs the wide block and never a wrong merge.
"""

import difflib
import json
from pathlib import Path

from _conflict_hunks import (
    BASE,
    OURS,
    THEIRS,
    hunks_of,
    segments,
    side_of,
    sides_of,
    splice,
)

_DECODER = json.JSONDecoder()

#: The field names an entry may be keyed by, in the order they are tried. A key
#: has to be a string every entry on every side carries, and unique within each
#: side — without that the alignment below cannot say which entry is which.
KEY_NAMES = ("id", "name", "key", "path", "slug")


def _elements(text: str) -> list[tuple[dict, int]] | None:
    """Each top-level JSON object in TEXT, with the offset just past its LAST
    LINE, or None when TEXT is not a run of whole sibling objects.

    The offset is a line boundary rather than the object's own last byte,
    because the pieces this returns are re-emitted around marker lines, and a
    marker git can read starts its own line. Two objects sharing one line
    therefore answer None, as do a value that is not an object and a trailing
    fragment that does not parse.
    """
    out: list[tuple[dict, int]] = []
    index = 0
    limit = len(text)
    while True:
        while index < limit and text[index] in " \t\r\n,":
            index += 1
        if index >= limit:
            break
        start = index
        try:
            value, index = _DECODER.raw_decode(text, start)
        except ValueError:
            return None
        if not isinstance(value, dict):
            return None
        if out and out[-1][1] > start:
            return None
        newline = text.find("\n", index)
        out.append((value, limit if newline < 0 else newline + 1))
    return out or None


def _slices(text: str, elements: list[tuple[dict, int]]) -> list[str]:
    """One text slice per element, together covering TEXT exactly.

    Slice N runs from the end of slice N-1 to the end of element N's line, so
    the separator and the indent between two entries ride with one of them and
    the concatenation is the original bytes back.
    """
    bounds = [0] + [end for _, end in elements]
    bounds[-1] = len(text)
    return [text[bounds[i] : bounds[i + 1]] for i in range(len(elements))]


class Array:
    """One version of a file that is a JSON array of objects, cut into the lines
    before the first entry, one text slice per entry, and the lines after the
    last one."""

    def __init__(self, prefix: str, suffix: str, elements, slices) -> None:
        self.prefix = prefix
        self.suffix = suffix
        self.elements = elements
        self.slices = slices

    def keys(self, name: str) -> list[str]:
        return [entry[name] for entry, _ in self.elements]


def _array(text: str) -> Array | None:
    """TEXT as an :class:`Array`, or None when it is not one this can cut.

    The brackets have to sit on lines of their own, because every piece below is
    re-emitted around a conflict marker and a marker starts its own line.
    """
    open_index = text.find("[")
    if open_index < 0 or text[:open_index].strip():
        return None
    newline = text.find("\n", open_index)
    if newline < 0 or text[open_index + 1 : newline].strip():
        return None
    close_index = text.rfind("]")
    if close_index < 0:
        return None
    line_start = text.rfind("\n", 0, close_index) + 1
    if text[line_start:close_index].strip():
        return None
    inner = text[newline + 1 : line_start]
    elements = _elements(inner)
    if elements is None:
        return None
    return Array(
        text[: newline + 1], text[line_start:], elements, _slices(inner, elements)
    )


def _key_name(sides: list[Array]) -> str | None:
    """The field the entries on every side can be aligned by, or None."""
    for name in KEY_NAMES:
        if all(
            isinstance(entry.get(name), str)
            for side in sides
            for entry, _ in side.elements
        ) and all(len(set(side.keys(name))) == len(side.elements) for side in sides):
            return name
    return None


def _groups(ours: list[str], theirs: list[str]) -> list[tuple[range, range]]:
    """The index ranges to emit, as one (ours, theirs) pair per group.

    A run both sides kept is broken down to ONE entry per group, which is where
    the narrowing comes from: each of those entries is then judged on its own,
    or leaves the conflict when the two sides wrote it identically. A run the
    sides disagree about stays whole, because nothing here can say which of its
    entries answers which.

    THE LAST GROUP CARRIES BOTH SIDES' LAST ENTRY, and trailing groups are merged
    back until it does. That is what keeps every mix of the sides valid JSON: an
    array's last entry is the one with no comma after it, so a group that ends
    one side's list while a later group still adds to the other side's would put
    a `}` and a `{` next to each other with nothing between them.
    """
    out: list[tuple[range, range]] = []
    matcher = difflib.SequenceMatcher(a=ours, b=theirs, autojunk=False)
    for tag, start_a, end_a, start_b, end_b in matcher.get_opcodes():
        if tag == "equal":
            out.extend(
                (range(i, i + 1), range(j, j + 1))
                for i, j in zip(range(start_a, end_a), range(start_b, end_b))
            )
        else:
            out.append((range(start_a, end_a), range(start_b, end_b)))
    while len(out) > 1 and not (out[-1][0] and out[-1][1]):
        (our_head, their_head), (our_tail, their_tail) = out[-2], out.pop()
        out[-1] = (
            range(our_head.start, our_tail.stop),
            range(their_head.start, their_tail.stop),
        )
    return out


def _taking(text: str, side: int) -> str:
    """TEXT with every conflict region resolved to SIDE — what the merge would be
    if a shard took that side everywhere. `BASE` gives the merge ancestor, which
    is only whole where every block carries a `|||||||` section."""
    return splice(
        text, {block.ordinal: side_of(block.text, side) for block in hunks_of(text)}
    )


def _ancestor(text: str) -> str | None:
    """TEXT's merge ancestor, or None when a block does not carry one. A block
    with no `|||||||` section would contribute nothing and the ancestor would
    silently lose those entries, which is a worse answer than no ancestor."""
    cut = [sides_of(block.text) for block in hunks_of(text)]
    if any(side is None or side.base is None for side in cut):
        return None
    return _taking(text, BASE)


def _emit(sides: dict[str, Array], name: str, markers) -> str | None:
    """The re-cut text: one conflict block per entry the two sides disagree
    about, and plain text for every entry they wrote alike. None when the cut
    leaves the file exactly as wide as it was."""
    ours, theirs, base = sides["ours"], sides["theirs"], sides.get("base")
    open_line, base_line, separator, close_line = markers
    our_keys, their_keys = ours.keys(name), theirs.keys(name)
    base_keys = [] if base is None else base.keys(name)
    out: list[str] = [ours.prefix]
    blocks = 0
    for our_range, their_range in _groups(our_keys, their_keys):
        our_text = "".join(ours.slices[i] for i in our_range)
        their_text = "".join(theirs.slices[j] for j in their_range)
        if our_text == their_text:
            out.append(our_text)
            continue
        blocks += 1
        # The ancestor entries this group is about, and only those: a `|||||||`
        # section carrying the whole list would put the wide region back.
        keys = {our_keys[i] for i in our_range} | {their_keys[j] for j in their_range}
        middle = ""
        if base is not None:
            middle = base_line + "".join(
                text for key, text in zip(base_keys, base.slices) if key in keys
            )
        out.append(open_line + our_text + middle + separator + their_text + close_line)
    out.append(ours.suffix)
    if blocks < 2 and len(out) <= 3:
        return None
    return "".join(out)


def narrow(path: str, text: str) -> str | None:
    """TEXT with its JSON-array conflicts re-cut per entry, or None when there is
    nothing here to narrow.

    None is also what a cut that failed the invariant answers, said out loud: the
    blocks git wrote then stand, which is today's behaviour and not a new failure.
    """
    if Path(path).suffix != ".json" or segments(text) is None:
        return None
    blocks = hunks_of(text)
    if not blocks:
        return None
    first = sides_of(blocks[0].text)
    if first is None:
        return None
    versions = {"ours": _taking(text, OURS), "theirs": _taking(text, THEIRS)}
    ancestor = _ancestor(text)
    if ancestor is not None:
        versions["base"] = ancestor
    sides = {}
    for label, version in versions.items():
        cut = _array(version)
        if cut is None:
            return None
        sides[label] = cut
    name = _key_name(list(sides.values()))
    if name is None:
        return None
    candidate = _emit(
        sides,
        name,
        (first.open_line, first.base_line, first.separator, first.close_line),
    )
    if candidate is None:
        return None
    if segments(candidate) is None or any(
        _taking(candidate, side) != versions[label]
        for side, label in ((OURS, "ours"), (THEIRS, "theirs"))
    ):
        print(
            f"::warning::{path}: cutting its conflict entry by entry changed the "
            "merge, so the blocks git wrote stand."
        )
        return None
    return candidate
