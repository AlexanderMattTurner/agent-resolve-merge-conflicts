#!/usr/bin/env python3
"""Auto-resolve merge conflicts — the FORMATTER-PADDING pre-pass.

PROBLEM CLASS — a markdown table whose cells a formatter pads to a fixed column
width turns a three-row disagreement into an eighty-row conflict. Prettier pads
every cell to the width of the widest cell in its column, so one added row with a
longer cell rewrites every other row. Git's line merge then sees ~80 changed
lines against ~80 and emits the whole table as one hunk — agent-glovebox #5697,
on PR #5684's `docs/configuration.md`, where the real disagreement was three rows
and the model was handed eighty.

The padding is derived from the widths of the rows beside it, so it is not
content. This pass takes the three sides out of the index — `git show :2:`, `:1:`
and `:3:` hand back the WHOLE ours, base and theirs files — strips every table
row's padding in each, re-merges them with `git merge-file`, and writes the
answer back INSIDE the conflict regions git marked. Whole files are what let it
read a criss-cross history and a non-`diff3` conflict style, neither of which
carries three usable sides in its markers.

The widths are not restored. Each row comes back with one space either side of
its cells, which is valid GFM, and the calling repository's own formatter hook
owns the widths again on the next commit.
"""

import difflib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _conflict_hunks import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    has_markers,
    hunk_line_ranges,
    hunks_of,
    is_marker_line,
    splice,
)
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PLAIN_MERGE_ATTRS,
    bind_repo,
    bound_repo,
    git,
    git_bytes,
    git_lines,
    merge_file_failed,
)

# The paths this pass reads as markdown. Anywhere else a line opening with `|` is
# not a table row, and stripping the space around its pipes would edit content.
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
# A file holding one path per line that this pass must leave alone, because
# another pass has already claimed it. prepare.sh names its deferred generated
# regions here: neither side of a derived region is the answer, so a text merge
# of one must not be staged ahead of the generator that owns it.
SKIP_FILE_ENV = "NARROW_SKIP_FILE"

# The index stages, in the order `git merge-file` takes its three files: ours is
# stage 2, the merge base stage 1, theirs stage 3.
_STAGES = (2, 1, 3)
# The names the re-merged markers carry, which double as the scratch file names.
_LABELS = ("ours", "base", "theirs")

# A table row: a line whose first and last non-blank characters are both `|`.
# At most three spaces of indentation — a fourth opens an indented code block,
# where a pipe-shaped line is code and its spacing is content, not padding.
_ROW_RE = re.compile(r"^ {0,3}\|.*\|[ \t]*$")
# The cell separator. A `\|` is an escaped pipe INSIDE a cell, not a separator.
_CELL_SEP_RE = re.compile(r"(?<!\\)\|")
# A delimiter row's cell — the `---`/`:--:` line under a table's header.
_DELIMITER_RE = re.compile(r"^:?-+:?$")
# A code fence, with the run of backticks or tildes and the info string apart.
_FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$")


def normalize_row(line: str) -> str:
    """LINE with one space around each cell, whatever width the formatter gave it.

    The leading indentation stays: a table nested under a list item is indented,
    and de-indenting it changes what the markdown says. Cells collapse only on a
    row that is ALL delimiter cells, so the widths of `:--------:` and `:-:` —
    one alignment at two table widths — stop reading as a change, while the
    common "no default" data cell `-` is content and comes back untouched.
    """
    body = line.rstrip("\r\n")
    newline = line[len(body) :]
    cells = _CELL_SEP_RE.split(body)
    inner = [cell.strip() for cell in cells[1:-1]]
    if inner and all(_DELIMITER_RE.match(cell) for cell in inner):
        inner = [_normalize_cell(cell) for cell in inner]
    return f"{cells[0]}| {' | '.join(inner)} |{newline}"


def _normalize_cell(text: str) -> str:
    """TEXT as one delimiter cell, cut to three characters keeping its colons."""
    left = ":" if text.startswith(":") else "-"
    right = ":" if text.endswith(":") else "-"
    return f"{left}-{right}"


def _is_row(body: str) -> bool:
    """BODY, one line without its newline, is a table row this pass may rewrite."""
    if not _ROW_RE.match(body):
        return False
    # `_CELL_SEP_RE` does not split an ESCAPED pipe, so a row whose closing `|`
    # is escaped (`| a | b \|`) leaves its last cell AFTER the final separator.
    # `normalize_row` drops what follows that separator, so refuse the line.
    return not _CELL_SEP_RE.split(body)[-1].strip()


def _is_delimiter_row(body: str) -> bool:
    """BODY is the `---`/`:--:` row that sits under a table's header row."""
    if not _is_row(body):
        return False
    inner = [cell.strip() for cell in _CELL_SEP_RE.split(body)[1:-1]]
    return bool(inner) and all(_DELIMITER_RE.match(cell) for cell in inner)


def _fence(body: str) -> tuple[str, int, str] | None:
    """BODY as a code fence — its character, its length, and its info string.

    None when BODY is no fence. A backtick fence's info string carries no
    backtick of its own, which is what tells one from a line of inline code.
    """
    match = _FENCE_RE.match(body)
    if match is None:
        return None
    marker, info = match.group("marker"), match.group("info")
    if marker[0] == "`" and "`" in info:
        return None
    return marker[0], len(marker), info


def _closes(fence: tuple[str, int, str], opener: tuple[str, int, str]) -> bool:
    """FENCE closes the block OPENER opened: the same character, at least as
    long, and no info string of its own."""
    return fence[0] == opener[0] and fence[1] >= opener[1] and not fence[2].strip()


def _code_lines(bodies: list[str]) -> list[bool]:
    """For each line of BODIES, whether a fenced code block holds it.

    A table someone WROTE OUT under a fence is an example, and every space in it
    is content. The state comes from the whole document, which is what holding
    all three sides buys: a conflict hunk on its own cannot see the fence that
    opened above it. Marker lines are read past, so a fence git cut in half
    still closes the block it opened.
    """
    out: list[bool] = []
    opener: tuple[str, int, str] | None = None
    for body in bodies:
        if is_marker_line(body):
            out.append(opener is not None)
            continue
        fence = _fence(body)
        out.append(opener is not None or fence is not None)
        if opener is None:
            opener = fence
        elif fence is not None and _closes(fence, opener):
            opener = None
    return out


def _opens_table(bodies: list[str], code: list[bool], pair: list[int]) -> bool:
    """The two indexes PAIR names are a table's header row and its delimiter row."""
    if len(pair) < 2:
        return False
    head, rule = pair
    return (
        not code[head]
        and not code[rule]
        and _is_row(bodies[head])
        and _is_delimiter_row(bodies[rule])
    )


def _table_rows(bodies: list[str], code: list[bool]) -> set[int]:
    """The indexes of BODIES that a GFM table's rows occupy.

    A table opens on a row followed by a delimiter row, and runs to the first
    line that is no row. Demanding that two-line opening is what tells a table
    from a paragraph line that merely starts with `|`, whose spacing is content.
    A conflict marker does not end a table: git cuts one in half, and the halves
    have to normalize alike or `_replacements` refuses the whole file.

    This reads the markdown itself rather than asking a parser. The resolver is
    cloned into a runner temp directory and nothing installs dependencies for
    it, so every script here imports the standard library and nothing else.
    """
    rows: set[int] = set()
    content = [index for index, body in enumerate(bodies) if not is_marker_line(body)]
    position = 0
    while position < len(content):
        if not _opens_table(bodies, code, content[position : position + 2]):
            position += 1
            continue
        while position < len(content):
            index = content[position]
            if code[index] or not _is_row(bodies[index]):
                break
            rows.add(index)
            position += 1
    return rows


def normalize_document(text: str) -> str:
    """TEXT with the formatter's padding out of every table row.

    LINE FOR LINE: a row comes back as one row, and every other line comes back
    byte for byte. That is what lets `_replacements` trace a line of the
    re-merged answer back to the line git wrote, and so what keeps this pass
    inside the regions the merge actually put in conflict.
    """
    lines = text.splitlines(keepends=True)
    bodies = [line.rstrip("\r\n") for line in lines]
    rows = _table_rows(bodies, _code_lines(bodies))
    return "".join(
        normalize_row(line) if index in rows else line
        for index, line in enumerate(lines)
    )


def _merge_file(sides: list[str]) -> str | None:
    """The three-way merge of SIDES (ours, base, theirs), or None when git could
    not do it. Conflicts are not a failure: the answer then carries markers.

    Bytes on both ends, because text mode decodes through universal newlines and
    would hand back a CRLF file with every line ending rewritten.

    `_relocation_port.apply_port` runs `merge-file` too, and the two share the
    exit-code contract through `_git_io` and nothing else: this one merges text
    in a throwaway directory, that one merges BYTES in the git dir and needs its
    own labels and orientation, so one wrapper would take every difference as an
    argument.
    """
    with tempfile.TemporaryDirectory() as scratch:
        paths = []
        for name, text in zip(_LABELS, sides):
            path = Path(scratch) / name
            path.write_bytes(text.encode("utf-8"))
            paths.append(str(path))
        done = subprocess.run(  # cwd-git-ok: merge-file reads only the three paths
            [
                "git",
                "merge-file",
                "-p",
                "--diff3",
                *[arg for label in _LABELS for arg in ("-L", label)],
                *paths,
            ],
            capture_output=True,
            check=False,
        )
    if merge_file_failed(done.returncode):
        return None
    return done.stdout.decode("utf-8")


def _anchors(
    before: list[str], after: list[str]
) -> tuple[dict[int, int], dict[int, int]]:
    """Where each line boundary of BEFORE sits in AFTER — the lowest answer the
    diff supports and the highest. A boundary the diff moved has neither.

    Two answers because a hunk's edges pull opposite ways: lines the merge added
    at a region's top belong to that region, and so do lines it added at the
    bottom. The file's own two ends are boundaries no diff reports, so they are
    seeded.
    """
    low: dict[int, int] = {0: 0}
    high: dict[int, int] = {}
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for tag, start, stop, other, _ in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(stop - start + 1):
            low.setdefault(start + offset, other + offset)
            high[start + offset] = other + offset
    low.setdefault(len(before), len(after))
    high[len(before)] = len(after)
    return low, high


def _replacements(
    stripped: str, merged: str, spans: list[tuple[int, int]]
) -> dict[int, str] | None:
    """What each conflict region of STRIPPED becomes in MERGED, region by region.

    INVARIANT — the answer is accepted only when putting it back into STRIPPED
    reproduces MERGED exactly. Both texts have the padding out, so every line
    OUTSIDE a region is the same in each; that equality is the proof that the
    re-merge changed nothing the conflict did not cover. None when a region's
    edges do not line up, or when the leftovers are not MERGED.
    """
    before = stripped.splitlines(keepends=True)
    after = merged.splitlines(keepends=True)
    low, high = _anchors(before, after)
    answer: dict[int, str] = {}
    for ordinal, (first, last) in enumerate(spans, 1):
        start, stop = low.get(first - 1), high.get(last)
        if start is None or stop is None:
            return None
        answer[ordinal] = "".join(after[start:stop])
    return answer if splice(stripped, answer) == merged else None


def _conflicted_lines(text: str) -> int:
    """How many lines of TEXT sit inside a conflict region, markers included."""
    return sum(len(hunk.text.splitlines()) for hunk in hunks_of(text))


def narrow_text(text: str, sides: list[str]) -> str | None:
    """TEXT — the file git left conflicted — re-merged from SIDES without the
    formatter padding, or None to leave it exactly as git wrote it.

    None whenever the re-merge is not an improvement: one git would not merge,
    one whose markers a later reader could not cut apart, one that does not line
    up with TEXT outside the conflict regions, and one whose conflict came back
    no smaller. That last case is what makes a table with no padding to strip a
    no-op — the re-merge reproduces the conflict git already wrote.
    """
    spans = hunk_line_ranges(text)
    if not spans:
        return None
    merged = _merge_file([normalize_document(side) for side in sides])
    if merged is None:
        return None
    replacements = _replacements(normalize_document(text), merged, spans)
    if replacements is None:
        return None
    answer = splice(text, replacements)
    # A re-merge that still conflicts must come back as regions a later reader
    # can cut apart again, or splicing it in would leave the file unreadable to
    # every pass downstream. `hunks_of` answers empty for markers it cannot parse.
    if has_markers(answer.encode("utf-8")) and not hunks_of(answer):
        return None
    if _conflicted_lines(answer) >= _conflicted_lines(text):
        return None
    return answer


def _skipped_paths() -> frozenset[str]:
    """The paths `SKIP_FILE_ENV` names, or none when it names no readable file."""
    name = os.environ.get(SKIP_FILE_ENV, "")
    if not name:
        return frozenset()
    try:
        text = Path(name).read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"::warning::narrow-padded-tables: could not read {SKIP_FILE_ENV}="
            f"{name} ({exc}); every conflict is narrowed as if it named none",
            file=sys.stderr,
        )
        return frozenset()
    return frozenset(line for line in text.splitlines() if line)


def _merge_is_plain(path: str) -> bool:
    """PATH's own merge is a plain text merge, so this pass may perform it.

    `git merge-file` dispatches on no attribute and no driver, so line-merging a
    path whose `.gitattributes` names one would apply exactly the policy that
    attribute exists to prevent. An unreadable answer refuses.
    """
    answer = git("check-attr", "merge", "--", path, check=False).strip()
    return answer.rsplit(": ", 1)[-1] in PLAIN_MERGE_ATTRS


def _index_sides(path: str) -> list[str] | None:
    """PATH's ours, base and theirs, as git recorded them in the index.

    None when any stage is missing. That is an add/add or a modify/delete, which
    has no three sides to re-merge and is no business of this pass.
    """
    sides = []
    for stage in _STAGES:
        blob = git_bytes("show", f":{stage}:{path}")
        if blob is None:
            return None
        sides.append(blob.decode("utf-8"))
    return sides


def unmerged_markdown() -> list[str]:
    """The markdown paths this merge left conflicted and this pass may narrow."""
    skipped = _skipped_paths()
    return [
        path
        for path in git_lines("diff", "--name-only", "--diff-filter=U")
        if Path(path).suffix in MARKDOWN_SUFFIXES
        and path not in skipped
        and _merge_is_plain(path)
    ]


def narrow_conflicts(paths: list[str]) -> tuple[list[str], list[str]]:
    """Re-merge each path's padded-table conflicts. Returns the paths this
    narrowed and, of those, the ones it resolved whole.

    A file left with no markers at all is STAGED here. Nothing about it is a
    judgement — it is git's own merge of the same rows with the derived widths
    taken out — and a marker-free file that stayed unmerged reads downstream like
    a modify/delete conflict, which is a different verdict entirely.
    """
    root = bound_repo()
    narrowed: list[str] = []
    resolved: list[str] = []
    for path in paths:
        file = root / path
        try:
            # `newline=""` on BOTH ends: the default translates every CRLF in the
            # file to a bare LF on the way in and writes it back out that way, so
            # a CRLF file would come back rewritten line by line, outside the
            # conflict as well as inside it.
            with file.open(encoding="utf-8", newline="") as handle:
                text = handle.read()
            sides = _index_sides(path)
        except (OSError, UnicodeDecodeError) as exc:
            print(
                f"::warning::narrow-padded-tables: could not read {path} ({exc}); "
                "it reaches the LLM exactly as git wrote it",
                file=sys.stderr,
            )
            continue
        if sides is None:
            continue
        answer = narrow_text(text, sides)
        if answer is None or answer == text:
            continue
        with file.open("w", encoding="utf-8", newline="") as handle:
            handle.write(answer)
        narrowed.append(path)
        if not has_markers(answer.encode("utf-8")):
            git("add", "--", path)
            resolved.append(path)
    return narrowed, resolved


def main() -> None:
    """Run the pass over the checkout this process sits in."""
    bind_repo(Path.cwd())
    narrowed, resolved = narrow_conflicts(unmerged_markdown())
    if narrowed:
        print(
            f"Re-merged {len(narrowed)} markdown conflict(s) without their "
            f"formatter padding: {' '.join(narrowed)}"
        )
    if resolved:
        print(
            f"{len(resolved)} of those held nothing but padding and resolved "
            f"outright, skipping the LLM: {' '.join(resolved)}"
        )


if __name__ == "__main__":
    main()
