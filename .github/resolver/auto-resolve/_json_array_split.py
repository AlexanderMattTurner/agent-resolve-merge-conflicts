"""Re-cutting a JSON array's conflicts entry by entry instead of line by line.

PROBLEM CLASS — a shard's whole assignment is one conflict block, so a block
that is a rewritten LIST is a block no shard finishes inside
`SHARD_TIMEOUT_SECONDS`. agent-glovebox#5644 records five runs that each handed
the same `.github/sbx-live/checks.json` back untouched. Git cuts a file on its
own LINE diff, so a block starts in the middle of one entry and ends in the
middle of another. This re-cuts the same merge on the array's element
boundaries, over the whole file rather than one block at a time, because a
block is a fragment that does not parse on its own.

INVARIANT — a shard takes either side of EACH block on its own, so the re-cut
has to be a merge of the same three versions under any MIX of those choices.
:func:`narrow` checks the two pure reconstructions, and :func:`_alignable`
refuses the two shapes a mix breaks: a key the sides hold in a different order,
and an ancestor key neither side still carries.

SECOND INVARIANT — every line git left OUTSIDE a block survives every resolution
of the re-cut. `_out_of_conflict` takes its spans from git's own merge of the two
parents and never from this file, so a line the re-cut pulls INSIDE a block is
one that guard still protects. :func:`_keeps_context` refuses that cut.
"""

import difflib
import json
from dataclasses import dataclass
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

#: The most blocks one file's re-cut may leave behind. Each becomes a `Work` in
#: `fanout.plan_work`, and the fan-out spends one shared wall-clock budget over
#: the files in list order, so an array cut past this bound starves the files
#: behind it. `_bounded` merges neighbouring groups until the count fits, which
#: costs width on that one file and nothing to the rest.
MAX_BLOCKS = 16


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


@dataclass(frozen=True)
class Array:
    """One version of a file that is a JSON array of objects, cut into the lines
    before the first entry, one text slice per entry, and the lines after the
    last one."""

    prefix: str
    suffix: str
    elements: list[tuple[dict, int]]
    slices: list[str]

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


def _alignable(sides: dict[str, Array], name: str) -> bool:
    """Whether ANY mix of per-block side choices over these groups is still a
    merge of the same three versions.

    Two shapes break that. A key both sides keep at a DIFFERENT position becomes
    a separate insert and a separate delete, so a mix that takes theirs at one
    and ours at the other emits the entry twice, or drops it. An ancestor key
    NEITHER side still carries — both sides renamed the same entry — belongs to
    no group, so its `|||||||` section would come out empty and the shard would
    read an edit as a deletion. Neither is answerable from the keys alone, so
    both refuse the file and leave git's wide blocks.
    """
    our_keys, their_keys = sides["ours"].keys(name), sides["theirs"].keys(name)
    shared = set(our_keys) & set(their_keys)
    if [k for k in our_keys if k in shared] != [k for k in their_keys if k in shared]:
        return False
    base = sides.get("base")
    return base is None or set(base.keys(name)) <= set(our_keys) | set(their_keys)


def _bounded(
    groups: list[tuple[range, range]], ours: list[str], theirs: list[str]
) -> list[tuple[range, range]]:
    """GROUPS with neighbours merged until at most `MAX_BLOCKS` of them differ.

    The differing groups are split into that many even buckets and each bucket
    is merged into one group, so the blocks stay spread over the file instead of
    one wide head block and a tail of single entries.
    """
    differ = [
        index
        for index, (our_range, their_range) in enumerate(groups)
        if "".join(ours[i] for i in our_range)
        != "".join(theirs[j] for j in their_range)
    ]
    if len(differ) <= MAX_BLOCKS:
        return groups
    out: list[tuple[range, range]] = []
    done = 0
    for bucket in range(MAX_BLOCKS):
        start = differ[len(differ) * bucket // MAX_BLOCKS]
        stop = differ[len(differ) * (bucket + 1) // MAX_BLOCKS - 1] + 1
        out.extend(groups[done:start])
        out.append(
            (
                range(groups[start][0].start, groups[stop - 1][0].stop),
                range(groups[start][1].start, groups[stop - 1][1].stop),
            )
        )
        done = stop
    out.extend(groups[done:])
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


def _context(text: str) -> list[str]:
    """TEXT's lines that sit OUTSIDE every conflict region — the ones `splice`
    copies verbatim, and the ones `_out_of_conflict` protects."""
    parts = segments(text)
    return (
        []
        if parts is None
        else [
            line
            for part in parts
            if isinstance(part, str)
            for line in part.splitlines(keepends=True)
        ]
    )


def _holds_in_order(want: list[str], have: list[str]) -> bool:
    """Whether every line of WANT appears in HAVE, in WANT's own order."""
    found = iter(have)
    return all(any(line == seen for seen in found) for line in want)


def _resolutions(text: str):
    """The files a fan-out could leave behind: the two whole-side ones and every
    single-block flip of each. The sample `_hunk_separable.separable` takes, for
    the same reason — a shard picks per block, and 2^n mixes is too many to walk,
    while a flip is what exposes the one block whose edges moved."""
    blocks = hunks_of(text)
    for pure, other in ((OURS, THEIRS), (THEIRS, OURS)):
        for flipped in range(-1, len(blocks)):
            yield splice(
                text,
                {
                    block.ordinal: side_of(
                        block.text, other if index == flipped else pure
                    )
                    for index, block in enumerate(blocks)
                },
            )


def _keeps_context(original: str, candidate: str) -> bool:
    """Whether every resolution of CANDIDATE still holds each line ORIGINAL left
    outside a conflict region.

    `_out_of_conflict` reverts, or reports, any line a resolution changed outside
    a conflict span, and it takes those spans from git's OWN merge of the two
    parents — never from the file this pass rewrote. Git aligns on the `},`
    between two entries, so the line that OPENS an entry can sit outside the
    block git cut while the entry sits inside it. A re-cut that swallows that
    line makes a correct per-entry resolution read as an edit to untouched
    context. The revert is then ambiguous, the run lands with auto-merge off and
    a person reads the delta — the handoff this pass exists to remove.

    A line walk and not `out_of_conflict_hunks` itself: that is `difflib` over
    the whole file once per block, and a 3400-line array measured 71 seconds of
    the fan-out's own budget.
    """
    context = _context(original)
    return all(
        _holds_in_order(context, resolved.splitlines(keepends=True))
        for resolved in _resolutions(candidate)
    )


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
    groups = _bounded(_groups(our_keys, their_keys), ours.slices, theirs.slices)
    for our_range, their_range in groups:
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
    if name is None or not _alignable(sides, name):
        return None
    candidate = _emit(
        sides,
        name,
        (first.open_line, first.base_line, first.separator, first.close_line),
    )
    if candidate is None:
        return None
    reason = ""
    if segments(candidate) is None or any(
        _taking(candidate, side) != versions[label]
        for side, label in ((OURS, "ours"), (THEIRS, "theirs"))
    ):
        reason = "changed the merge"
    elif not _keeps_context(text, candidate):
        reason = "moved a line git left outside every conflict region inside one"
    if reason:
        print(
            f"::warning::{path}: cutting its conflict entry by entry {reason}, so "
            "the blocks git wrote stand."
        )
        return None
    return candidate


def narrow_json_conflicts(file: str) -> None:
    """Re-cut FILE's conflict blocks on a JSON array's own entry boundaries.

    Written back to the worktree, because every later stage reads the file from
    disk: the shard's block, the splice that puts its answer back, and the
    marker sweep that says what is left. Nothing else moves — the re-cut
    resolves to the same bytes on either side, which `narrow` checks first.
    """
    try:
        text = Path(file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    narrowed = narrow(file, text)
    if narrowed is not None:
        Path(file).write_text(narrowed, encoding="utf-8")
