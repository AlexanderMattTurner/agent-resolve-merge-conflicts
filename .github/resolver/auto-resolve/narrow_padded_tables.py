#!/usr/bin/env python3
"""Auto-resolve merge conflicts — the FORMATTER-PADDING pre-pass.

PROBLEM CLASS — a markdown table whose cells a formatter pads to a fixed column
width turns a three-row disagreement into an eighty-row conflict. Prettier pads
every cell to the width of the widest cell in its column, so one added row with a
longer cell rewrites every other row of the table. Git's line merge then sees ~80
changed lines against ~80 changed lines and emits the whole table as one hunk —
agent-glovebox #5697, on PR #5684's `docs/configuration.md`, where the real
disagreement was three rows and the model was handed eighty.

The padding is not content: it is derived from the widths of the rows beside it.
So this pass strips the padding from all three sides of such a hunk, re-merges
the stripped rows with `git merge-file`, and puts the answer back. A hunk whose
real edits do not overlap resolves outright, with both sides' rows kept; one
whose edits do overlap keeps markers around the rows that truly disagree.

The widths are not restored here. Each row comes back with one space either side
of its cells, which is valid GFM and renders identically, and the calling
repository's own formatter hook owns the widths again on the next commit.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _conflict_hunks import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    BASE,
    OURS,
    THEIRS,
    Hunk,
    has_markers,
    hunks_of,
    segments,
    side_of,
    splice,
)
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    bind_repo,
    bound_repo,
    git,
    git_lines,
)

# The paths this pass reads as markdown. Anywhere else a line opening with `|` is
# not a table row, and stripping the space around its pipes would edit content.
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

# A table row: a line whose first and last non-blank characters are both `|`.
_ROW_RE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
# The cell separator. A `\|` is an escaped pipe INSIDE a cell, not a separator.
_CELL_SEP_RE = re.compile(r"(?<!\\)\|")
# A delimiter row's cell — the `---`/`:--:` line under a table's header.
_DELIMITER_RE = re.compile(r"^:?-+:?$")
# git's own exit codes for `merge-file`: 0 clean, 1..127 that many conflicts,
# anything above an error, and a negative value a signal.
_MERGE_FILE_MAX_CONFLICTS = 127
# The base section's marker, which tells a diff3 block from a two-sided one.
_BASE_MARKER_RE = re.compile(r"^\|{7}(?: |$)", re.MULTILINE)


def normalize_row(line: str) -> str:
    """LINE with one space around each cell, whatever width the formatter gave it.

    A delimiter cell collapses to three characters keeping its alignment colons,
    so `:--------:` and `:-:` — the same alignment at two table widths — become
    one string that the three-way merge no longer reads as a change.
    """
    body = line.rstrip("\n")
    newline = line[len(body) :]
    cells = _CELL_SEP_RE.split(body)
    inner = [_normalize_cell(cell.strip()) for cell in cells[1:-1]]
    return f"| {' | '.join(inner)} |{newline}"


def _normalize_cell(text: str) -> str:
    """TEXT as one cell's content, with a delimiter cell cut to three characters."""
    if not _DELIMITER_RE.match(text):
        return text
    left = ":" if text.startswith(":") else "-"
    right = ":" if text.endswith(":") else "-"
    return f"{left}-{right}"


def normalize_side(text: str) -> str | None:
    """TEXT with every table row's padding stripped, or None when TEXT is not all
    table. A blank line is kept as it is — it separates two tables and carries no
    padding. Any other line means the region is prose or code the padding argument
    says nothing about, so this pass leaves the whole hunk alone."""
    out = []
    for line in text.splitlines(keepends=True):
        if not line.strip():
            out.append(line)
        elif _ROW_RE.match(line.rstrip("\n")):
            out.append(normalize_row(line))
        else:
            return None
    return "".join(out)


def _labels(block: str) -> list[str] | None:
    """The three names on BLOCK's own marker lines, so the re-merged block reads
    with the same labels git wrote. None when the block is not diff3: with no base
    section there is no ancestor to merge against, and a re-merge would report
    every row as a change both sides made."""
    if not _BASE_MARKER_RE.search(block):
        return None
    # A criss-cross history writes the ancestor's OWN conflict into the base
    # section. `side_of` drops those nested lines, so the base this pass would
    # merge against is not the ancestor git recorded. Leave such a block whole.
    if sum(line.startswith("<<<<<<<") for line in block.splitlines()) > 1:
        return None
    found = {}
    for line in block.splitlines():
        for marker, key in (("<<<<<<<", OURS), ("|||||||", BASE), (">>>>>>>", THEIRS)):
            if line.startswith(marker) and key not in found:
                found[key] = line[len(marker) :].strip()
    if len(found) != 3:
        return None
    return [found[OURS], found[BASE], found[THEIRS]]


def _merge_file(sides: list[str], labels: list[str]) -> str | None:
    """The three-way merge of SIDES (ours, base, theirs), or None when git could
    not do it. Conflicts are not a failure: the answer then carries markers."""
    with tempfile.TemporaryDirectory() as scratch:
        paths = []
        for name, text in zip(("ours", "base", "theirs"), sides):
            path = Path(scratch) / name
            path.write_text(text, encoding="utf-8")
            paths.append(str(path))
        done = subprocess.run(  # cwd-git-ok: merge-file reads only the three paths
            [
                "git",
                "merge-file",
                "-p",
                "--diff3",
                *[arg for label in labels for arg in ("-L", label)],
                *paths,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if done.returncode < 0 or done.returncode > _MERGE_FILE_MAX_CONFLICTS:
        return None
    return done.stdout


def _conflicted_lines(text: str) -> int:
    """How many lines of TEXT sit inside a conflict region, markers included."""
    return sum(len(hunk.text.splitlines()) for hunk in hunks_of(text))


def narrow_hunk(block: str) -> str | None:
    """BLOCK re-merged without its formatter padding, or None to leave it alone.

    None whenever the re-merge is not an improvement — a block that is not all
    table, one git would not merge, and one whose conflict came back no smaller.
    That last case is what makes a hunk with no padding to strip a no-op: the
    re-merge reproduces the block git already wrote.
    """
    labels = _labels(block)
    if labels is None:
        return None
    sides = [normalize_side(side_of(block, which)) for which in (OURS, BASE, THEIRS)]
    if any(side is None for side in sides):
        return None
    merged = _merge_file([side for side in sides if side is not None], labels)
    if merged is None:
        return None
    # A re-merge that still conflicts must come back as regions a later reader can
    # cut apart again, or splicing it in would leave the file unreadable to every
    # pass downstream. `hunks_of` answers empty for markers it cannot parse.
    if has_markers(merged.encode("utf-8")) and not hunks_of(merged):
        return None
    if _conflicted_lines(merged) >= len(block.splitlines()):
        return None
    return merged


def narrow_text(text: str) -> str | None:
    """TEXT with every padded-table hunk re-merged, or None when none was."""
    parts = segments(text)
    if parts is None:
        return None
    narrowed = {}
    for part in parts:
        if not isinstance(part, Hunk):
            continue
        answer = narrow_hunk(part.text)
        if answer is not None:
            narrowed[part.ordinal] = answer
    return splice(text, narrowed) if narrowed else None


def unmerged_markdown() -> list[str]:
    """The markdown paths this merge left conflicted."""
    return [
        path
        for path in git_lines("diff", "--name-only", "--diff-filter=U")
        if Path(path).suffix in MARKDOWN_SUFFIXES
    ]


def narrow_conflicts(paths: list[str]) -> tuple[list[str], list[str]]:
    """Re-merge each path's padded-table hunks. Returns the paths this narrowed
    and, of those, the ones it resolved whole.

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
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        answer = narrow_text(text)
        if answer is None or answer == text:
            continue
        file.write_text(answer, encoding="utf-8")
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
