"""PROBLEM CLASS — a merge that keeps BOTH parents' version of one statement,
where the two say the opposite of each other.

Every line of such a merge traces to a parent, so `_neither_side` passes it and
the self-review reads a delta in which no line is new. Only running the program
sees it. On agent-glovebox #5606 the base branch added
`assert f"Read({path})" not in deny` to a test and the pull request added
`assert f"Read({path})" in deny` to the same loop. git merged both cleanly, the
merged loop asserted a thing and its negation, and no deny list could pass it.

This reads the two parents' ADDED lines, not a conflict region: the merge that
bit was a clean one, and a check gated on markers would have seen nothing. Two
added lines are a contradicting pair when they carry the same indentation, sit
within `_SEAM_LINES` of each other in the merged file, and differ only in a
negation. Both filters are precision, not correctness — the same statement and
its negation legitimately live in two different functions of one file.

Reported, never refused, for the reason `_neither_side` reports: `land` names
the lines and turns auto-merge off, and a resolution whose other hunks are sound
still lands.
"""

import io
import re
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import git  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _neither_side import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    describe,
)

# How far apart two lines may sit in the merged file and still be read as one
# seam. A contradiction the merge created is adjacent text: the parents edited
# the same few lines. Further apart, two functions of one file are the likelier
# reading, and this reports nothing.
_SEAM_LINES = 10
# Paths scanned, of those BOTH parents added lines to. A merge of two long
# branches can reach thousands, and each costs one file read.
_MAX_PATHS = 200

# Each rewrite deletes one way of saying "not", so a line and its negation
# canonicalise to the same text. Order matters: `not in` and `is not` are read
# before the bare `not` that would otherwise split them.
_NEGATIONS = (
    (re.compile(r"\bis\s+not\b"), " is "),
    (re.compile(r"\bnot\s+in\b"), " in "),
    (re.compile(r"\bnot\b"), " "),
    (re.compile(r"!=="), "==="),
    (re.compile(r"!="), "=="),
    (re.compile(r"(?<![=!<>])!(?=[\w(\[])"), ""),
)
_COMMENT_STARTS = ("#", "//", "/*", "*", "<!--", "--")
# A line that opens or closes a block states nothing a negation can turn over,
# and negation-stripping makes such lines collide. Length says nothing here:
# `if x:` is five characters of executable syntax.
_STRUCTURE_ONLY = frozenset(
    {"else:", "try:", "finally:", "else", "do", "then", "fi", "esac", "done", "end"}
)
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
# What a masked string literal or comment leaves behind: one character no
# source line holds, so the negations above cannot match through it and two
# lines differing only inside a literal still differ.
_MASKED = "\x00"
_QUOTES = ("'", '"', "`")


def _masked(line: str, suffix: str) -> str | None:
    """LINE with every string literal and comment replaced, or None when no
    reader here can tell this line's code from its text.

    A `!` inside `run("rm -rf !important")` is text, and `not` after a `#` is
    prose; both read as negations without this, and both then pair with the
    line that merely lacks them."""
    if suffix == ".py":
        return _masked_python(line)
    return None if any(quote in line for quote in _QUOTES) else line


def _masked_python(line: str) -> str | None:
    """LINE with its STRING, FSTRING and COMMENT tokens masked.

    None when the line does not tokenize on its own — a continuation, or half an
    open bracket — because a partial parse names the wrong tokens."""
    out: list[str] = []
    column = 0
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(line).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    for token in tokens:
        if token.start[0] != 1 or token.type in _NON_TEXT_TOKENS:
            continue
        name = tokenize.tok_name[token.type]
        # A comment is DROPPED, never masked: it always trails, so a placeholder
        # would leave the commented line keying apart from the same line without
        # one. A literal sits mid-expression, so it keeps its place.
        if name == "COMMENT":
            break
        out.append(" " * (token.start[1] - column))
        out.append(
            _MASKED if name == "STRING" or name.startswith("FSTRING") else token.string
        )
        column = token.end[1]
    return "".join(out)


_NON_TEXT_TOKENS = frozenset(
    {
        tokenize.ENDMARKER,
        tokenize.NEWLINE,
        tokenize.NL,
        tokenize.INDENT,
        tokenize.DEDENT,
    }
)


def _rewritten(line: str) -> tuple[str, int]:
    """LINE with every negation removed and its whitespace flattened, and how
    many negations that removal consumed."""
    text = line.strip()
    marks = 0
    for pattern, replacement in _NEGATIONS:
        text, hits = pattern.subn(replacement, text)
        marks += hits
    return " ".join(text.split()), marks


def polarity_free(line: str) -> str:
    """LINE with every negation removed and its whitespace flattened.

    Two lines share this form exactly when one asserts what the other denies."""
    return _rewritten(line)[0]


def negation_marks(line: str) -> int:
    """How many negations LINE carries.

    Two lines are each other's negation only when this count differs. A pair
    that differs only in spacing shares the polarity-free form without any
    negation between them, and states one thing twice."""
    return _rewritten(line)[1]


def _negations_differ(ours: str, theirs: str, suffix: str) -> bool:
    """Whether OURS and THEIRS carry a different number of negations, counted
    over code alone.

    Both lines already key alike, so a mask that answers None for either leaves
    no pair to report."""
    ours_code, theirs_code = _masked(ours, suffix), _masked(theirs, suffix)
    if ours_code is None or theirs_code is None:
        return False
    return negation_marks(ours_code) != negation_marks(theirs_code)


def _flat(line: str) -> str:
    """LINE with its whitespace flattened and its polarity left alone."""
    return " ".join(line.split())


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _carries_a_statement(stripped: str) -> bool:
    """Whether STRIPPED holds executable syntax for a negation to turn over. A
    bare `)` or `}` holds none, and a lone block keyword states nothing."""
    return stripped not in _STRUCTURE_ONLY and bool(_IDENTIFIER.search(stripped))


def _keyed(added: set[str], suffix: str) -> dict[tuple[str, str], set[str]]:
    """The added lines that can carry a contradiction, keyed by indentation and
    polarity-free text. A comment is dropped: prose says the opposite of prose
    all the time, and no program reads it. So is a line whose code this suffix
    cannot be separated from its text."""
    keyed: dict[tuple[str, str], set[str]] = {}
    for line in added:
        stripped = line.strip()
        if not _carries_a_statement(stripped) or stripped.startswith(_COMMENT_STARTS):
            continue
        code = _masked(line, suffix)
        if code is None or not _carries_a_statement(code.strip()):
            continue
        keyed.setdefault((_indent(line), polarity_free(code)), set()).add(
            line.rstrip("\n")
        )
    return keyed


def contradicting_line_numbers(
    head_added: set[str], base_added: set[str], merged_text: str, suffix: str
) -> list[int]:
    """The 1-based MERGED_TEXT line numbers of every contradicting pair.

    SUFFIX is the file's, and has no default: it decides whether a line's code
    can be told from its text, and a caller that omitted it would silently get
    no report for every line carrying a quote.

    A pair counts only when BOTH of its lines survived into the merged text: one
    side's addition alone is a resolution that chose, which is the answer this
    check wants.

    A line BOTH parents added is dropped first. One parent that added a whole
    block asserting a thing and then its negation — a cherry-pick, or a rename
    read as a new file — carries the pair on its own, and the merge of the two
    creates nothing."""
    only_head, only_base = head_added - base_added, base_added - head_added
    head_keyed, base_keyed = _keyed(only_head, suffix), _keyed(only_base, suffix)
    merged = [line.rstrip("\n") for line in merged_text.splitlines()]
    numbers: set[int] = set()
    for key in head_keyed.keys() & base_keyed.keys():
        for ours in head_keyed[key]:
            for theirs in base_keyed[key]:
                if _negations_differ(ours, theirs, suffix):
                    numbers.update(_seam(merged, ours, theirs))
    return sorted(numbers)


def _seam(merged: list[str], ours: str, theirs: str) -> set[int]:
    """The line numbers OURS and THEIRS occupy in MERGED, when both are there and
    close enough together to read as one seam. Empty otherwise.

    Whitespace-flattened, not exact: the repo's hooks and the post-merge repair
    both reformat the merged tree before this runs, and a re-spaced line is the
    same statement."""
    ours, theirs = _flat(ours), _flat(theirs)
    here = [n for n, line in enumerate(merged, start=1) if _flat(line) == ours]
    there = [n for n, line in enumerate(merged, start=1) if _flat(line) == theirs]
    return {
        number
        for one in here
        for other in there
        if abs(one - other) <= _SEAM_LINES
        for number in (one, other)
    }


def added_lines(base: str, side: str) -> dict[str, set[str]]:
    """PATH -> every line SIDE added to it since BASE.

    `--unified=0` so the output carries no context to mistake for an addition,
    and `--find-renames` so a renamed file reads as a rename rather than as a
    new file whose whole inherited body is an addition. `--no-renames` is what
    produces that reading, and it makes a pair the ancestor already carried look
    like one this merge created.

    The `+++` header is told from a hunk line by POSITION, not by its text: a
    file whose own content holds `++ x` prints `+++ x` inside a hunk, and reading
    that as a header would file the rest of the file's additions under a path
    nothing in the tree carries."""
    added: dict[str, set[str]] = {}
    name = ""
    in_hunk = False
    diff = git(
        "-c",
        "core.quotePath=false",
        "diff",
        "--unified=0",
        "--no-color",
        "--find-renames",
        f"{base}..{side}",
    )
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            name, in_hunk = "", False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and name and line.startswith("+"):
            added.setdefault(name, set()).add(line[1:])
        elif not in_hunk and line.startswith("+++ "):
            target = line[4:]
            name = "" if target == "/dev/null" else target.removeprefix("b/")
    return added


def unions_that_contradict(head: str, base: str) -> dict[str, str]:
    """PATH -> the range list of its lines where the merge kept both parents'
    version of one statement, and the two are each other's negation.

    Read from the WORKTREE, which is the tree the merge commit takes. A path the
    resolution deleted has nothing to report."""
    merge_base = git("merge-base", head, base).strip()
    head_added = added_lines(merge_base, head)
    base_added = added_lines(merge_base, base)
    both = sorted(set(head_added) & set(base_added))
    if len(both) > _MAX_PATHS:
        print(
            f"::warning::both parents added lines to {len(both)} paths; the "
            f"contradicting-union check read the first {_MAX_PATHS}."
        )
        both = both[:_MAX_PATHS]
    found: dict[str, str] = {}
    for name in both:
        path = Path(name)
        if not path.is_file():
            continue
        try:
            merged_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        numbers = contradicting_line_numbers(
            head_added[name], base_added[name], merged_text, path.suffix
        )
        if numbers:
            found[name] = describe(numbers)
    return found


class ContradictingUnionReport:
    """The APPLICATION of the analysis above to one bundle step.

    A mixin for the reason `NeitherSideReport` is one: it reads the step's own
    two parents and the tree the step is about to commit."""

    def report_contradicting_unions(self) -> None:
        """Name every seam where the merge kept a statement and its negation, and
        hand the list to `land` so auto-merge goes off.

        Run over the tree as it will be COMMITTED, for the reason the
        neither-side report runs there: the repo's hooks and the post-merge
        check's repair pass both rewrite files and move every line below them."""
        for name, ranges in sorted(
            unions_that_contradict(self.checked_out_head, self.merge_base_side).items()
        ):
            self.contradicting_lines.append(f"{name}\t{ranges}")
            print(
                f"::warning::the merge kept both parents' version of line(s) "
                f"{ranges} of '{name}', and the two are each other's negation. "
                "Every line traces to a parent, so no conflict and no delta "
                "review names this: the merge is landing with auto-merge off."
            )
