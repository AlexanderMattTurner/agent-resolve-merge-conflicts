"""PROBLEM CLASS — cutting a conflicted file into the independent regions git
marked, and putting a resolved region back without touching the rest.

A resolver that hands a model a whole conflicted file gets a whole file back,
and pays for that twice. It pays in wall clock: PR #3826's shard understood a
947-line file's conflicts in 92 seconds and then spent every remaining second
rewriting all 947 lines, so the per-shard timeout killed it mid-write. It pays
in trust: the merged file is the one channel that can introduce content present
in NEITHER parent, and a model rewriting lines nobody put in conflict can change
them with nothing to compare against.

Splitting the file removes both. Each region is resolved on its own, and `splice`
copies every line OUTSIDE the regions byte for byte, so an untouched line cannot
change however the model behaves.

The markers are line-oriented, so this is a line state machine rather than a
regex over the whole text. The pattern that opens and closes a region is the one
in .github/resolver/lib/shared-names.json, which bundle.py and lib.sh also read.
"""

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

_SHARED_NAMES = json.loads(
    (Path(__file__).resolve().parent.parent / "lib" / "shared-names.json").read_text(
        encoding="utf-8"
    )
)
# The four marker spellings, each anchored to the start of its own line.
_MARKER_RE = re.compile(_SHARED_NAMES["auto_resolve"]["conflict_marker_re"])
_OPEN = "<<<<<<<"
_CLOSE = ">>>>>>>"
_BASE_MARKER = "|||||||"
_SEPARATOR = "======="

# Which side of a block `side_of` keeps. _BASE is not a side: the `|||||||`
# section is the merge ancestor, so it belongs to neither and is always dropped.
OURS, THEIRS, _BASE = 0, 1, -1


@dataclass(frozen=True)
class Hunk:
    """One conflict region: its 1-based position in the file, how many regions
    the file has in all, and the block git wrote, markers included. `total` rides
    along because every consumer that has a region also needs to say which of how
    many it is, and re-counting at each of them is where the two can disagree."""

    ordinal: int
    total: int
    text: str


_MARKER_BYTES_RE = re.compile(
    _SHARED_NAMES["auto_resolve"]["conflict_marker_re"].encode("utf-8"), re.MULTILINE
)


def has_markers(data: bytes) -> bool:
    """Whether DATA still carries an unresolved conflict marker. Bytes, so a
    resolution in any encoding is scanned without a decode that could throw."""
    return bool(_MARKER_BYTES_RE.search(data))


def _outer_markers(lines: Iterable[str]) -> Iterator[tuple[str, str | None]]:
    """Each line with the OUTER conflict marker it is, or None when it is neither:
    an ordinary line, or a marker of a NESTED conflict.

    A merge whose virtual ancestor itself conflicted writes that ancestor's own
    markers into the `|||||||` section — the "Temporary merge branch" blocks a
    criss-cross history produces. They delimit no region a caller puts back, so
    they are `nested` and read as the base text they are. An opening marker
    anywhere ELSE inside a block is `malformed`: the text after it belongs to no
    side, so no caller can hand it back.
    """
    inside = in_base = False
    depth = 0
    for line in lines:
        if not _MARKER_RE.match(line):
            yield line, None
        elif line.startswith(_OPEN):
            if not inside:
                inside, in_base, depth = True, False, 0
                yield line, "open"
            elif in_base:
                depth += 1
                yield line, "nested"
            else:
                yield line, "malformed"
        elif not inside:
            yield line, None
        elif line.startswith(_CLOSE):
            if depth:
                depth -= 1
                yield line, "nested"
            else:
                inside = False
                yield line, "close"
        elif depth:
            yield line, "nested"
        elif line.startswith(_BASE_MARKER):
            in_base = True
            yield line, "base"
        else:
            # A matched marker that is neither an open/close nor the base is the
            # separator by exhaustion — _MARKER_RE owns no fifth spelling.
            in_base = False
            yield line, "separator"


def side_of(block: str, which: int) -> str:
    """BLOCK's WHICH side, with every marker line and the base section dropped.

    This is what a shard choosing that side would leave behind, so a caller can
    splice the choice back and ask whether the file still holds together.
    """
    out: list[str] = []
    keep = OURS
    for line, marker in _outer_markers(block.splitlines(keepends=True)):
        if marker is None:
            if keep == which:
                out.append(line)
        elif marker == "nested":
            continue
        elif marker == "base":
            keep = _BASE
        elif marker == "separator":
            keep = THEIRS
        else:
            keep = OURS
    return "".join(out)


def segments(text: str) -> list[str | Hunk] | None:
    """TEXT as an alternating run of untouched strings and conflict Hunks, or
    None when the markers do not nest into regions this can put back.

    None is a REFUSAL, not an empty answer: a caller that resolves hunk by hunk
    has to fall back to resolving the file whole, and an empty list would tell it
    the file is already clean. Both malformed cases produce it — an opening
    marker with no closing one, and a second opening marker inside an open
    region's OWN side — because in each the text between the markers is not a
    region this can hand back. A nested conflict inside the `|||||||` section is
    neither: `_outer_markers` reads it as the base text it is.
    """
    parts: list[str | Hunk] = []
    plain: list[str] = []
    block: list[str] | None = None
    ordinal = 0
    for line, marker in _outer_markers(text.splitlines(keepends=True)):
        if marker == "malformed":
            return None
        if marker == "open":
            parts.append("".join(plain))
            plain = []
            block = [line]
            continue
        if block is None:
            plain.append(line)
            continue
        block.append(line)
        if marker == "close":
            ordinal += 1
            parts.append(Hunk(ordinal, 0, "".join(block)))
            block = None
    if block is not None:
        return None
    parts.append("".join(plain))
    # The total is only known once the whole file is read, so it is stamped here
    # rather than guessed while the regions are still being found.
    return [
        replace(part, total=ordinal) if isinstance(part, Hunk) else part
        for part in parts
    ]


def hunks_of(text: str) -> list[Hunk]:
    """Every conflict region in TEXT, in file order. Empty for a file with no
    markers AND for one whose markers do not parse — the caller treats both the
    same way, by resolving the file as a whole."""
    parts = segments(text)
    return [] if parts is None else [part for part in parts if isinstance(part, Hunk)]


def hunk_line_ranges(text: str) -> list[tuple[int, int]]:
    """The 1-based, inclusive line range of every conflict Hunk in TEXT, in file
    order — where a human opens the file to read the region a shard judged,
    instead of scanning the whole file for it. Empty wherever `hunks_of` is."""
    parts = segments(text)
    if parts is None:
        return []
    ranges: list[tuple[int, int]] = []
    line = 1
    for part in parts:
        content = part.text if isinstance(part, Hunk) else part
        span = len(content.splitlines())
        if isinstance(part, Hunk):
            ranges.append((line, line + span - 1))
        line += span
    return ranges


def splice(text: str, resolved: dict[int, str]) -> str:
    """TEXT with each resolved region replaced by its resolution.

    INVARIANT: every line outside a conflict region is copied verbatim, and a
    region with no resolution keeps its markers. That is what makes an
    unresolved hunk visible to the downstream marker sweep instead of silently
    dropped, and what stops a resolution of one region from editing another.

    Rebuilt from the segments rather than spliced by line number, so no offset
    arithmetic can drift as earlier regions change length.
    """
    parts = segments(text)
    if parts is None:
        raise ValueError("cannot splice a file whose conflict markers do not parse")
    out = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
            continue
        replacement = resolved.get(part.ordinal)
        out.append(part.text if replacement is None else replacement)
    return "".join(out)
