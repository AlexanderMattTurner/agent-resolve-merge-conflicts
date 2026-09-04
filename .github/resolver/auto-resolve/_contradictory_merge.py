"""PROBLEM CLASS — the merge is broken and every one of its lines traces to a
parent, so no provenance check and no delta review names it.

`_out_of_conflict` and `_neither_side` ask where each line came from. A merge
can answer that for every line and still be wrong, because the lines that
SURVIVED contradict each other. Three shapes reach this, each from a real
resolution:

* a two-sided rename, split. One parent added a module-level alias and pointed
  its caller at it; the other kept the plain call. The merge kept both, so the
  alias has no reader, two fixtures patch a name nothing calls, and six tests
  sit out a real cooldown (agent-glovebox #5568).
* a revert, undone. Each parent's own commit deleted a line, and the merge puts
  it back (agent-glovebox #5641).
* a statement beside its negation. One parent added `assert x in deny`, the
  other `assert x not in deny`, git merged both cleanly, and no deny list
  satisfies the pair (agent-glovebox #5606).

Python only, through `ast` and whole-line comparison, matching
`dropped_name_seams.py`'s contract: a language with no parser here is out of
scope, never a guess. Every check reads the merge base as well as both parents,
so a finding names a line the MERGE produced rather than one a branch carried.

Reported, never refused, for the reason `_neither_side` reports. Each check
below is a heuristic with tuned precision filters, so a refusal on a false
positive throws away a resolution the model was already billed for and hands a
human the raw conflict as well. `land` names the findings and turns auto-merge
off, and the pull request's own checks read exactly this tree.
"""

import ast
import io
import re
import sys
import tokenize
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    git,
    git_result,
)
from _neither_side import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    describe,
)
from dropped_name_seams import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    module_level_identifiers,
)

# A resurrected line has to carry enough text that its reappearance means
# something. Below this a merge legitimately repeats `return`, `else:` or a
# closing bracket, and reporting one says nothing about the resolution.
_MIN_RESURRECTED_LINE = 12
# How far apart two lines may sit in the merged file and still read as one seam.
# A contradiction the merge created is adjacent text, because the parents edited
# the same few lines. Further apart, two functions of one file are the likelier
# reading, and this reports nothing.
_SEAM_LINES = 10
# Paths read for a contradicting union, of those BOTH parents added lines to. A
# merge of two long branches can reach thousands, and each costs one file read.
_MAX_PATHS = 200
# Names one finding quotes, matching `_neither_side`'s range count.
_NAMES_SHOWN = 5

# Each rewrite deletes one way of saying "not", so a line and its negation
# canonicalise to the same text. Order matters: `not in` and `is not` are read
# before the bare `not` that would otherwise split them.
_NEGATIONS = (
    (re.compile(r"\bis\s+not\b"), " is "),
    (re.compile(r"\bnot\s+in\b"), " in "),
    (re.compile(r"\bnot\b"), " "),
    (re.compile(r"!="), "=="),
)
# A line that opens or closes a block states nothing a negation can turn over,
# and negation-stripping makes such lines collide. Length says nothing here:
# `if x:` is five characters of executable syntax.
_STRUCTURE_ONLY = frozenset({"else:", "try:", "finally:"})
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
# What a masked string literal leaves behind: one character no source line
# holds, so the negations above cannot match through it and two lines differing
# only inside a literal still differ.
_MASKED = "\x00"
_NON_TEXT_TOKENS = frozenset(
    {
        tokenize.ENDMARKER,
        tokenize.NEWLINE,
        tokenize.NL,
        tokenize.INDENT,
        tokenize.DEDENT,
    }
)
# What each kind says in the job log. `land` renders its own text from the
# sidecar; this is what a maintainer reading the resolve step's output sees.
_SAID = {
    "orphaned-binding": (
        "the resolution left {detail} in '{name}' defined and unread, and a "
        "parent that added one of these names did read it."
    ),
    "resurrected-line": (
        "line(s) {detail} of '{name}' came back, and every parent's own commit "
        "deleted them."
    ),
    "contradicting-union": (
        "the merge kept both parents' version of line(s) {detail} of '{name}', "
        "and the two are each other's negation."
    ),
}


def _parse(text: str | None) -> ast.Module | None:
    """TEXT as a module, or None when it is absent or does not parse.

    An unparseable side is the ordinary case, not an error: it still carries
    conflict markers, or it is a file mid-port. This analysis has nothing to say
    about one, and a half-parsed comparison misattributes what it finds."""
    if text is None:
        return None
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def names_read(tree: ast.AST) -> set[str]:
    """Every name TREE reads, in one walk.

    A load of a bare name, a `global` declaration and a string constant all
    count. The last covers the two ways a name is read with no identifier node —
    an `__all__` entry, and a `setattr` by name, which is how a test fixture
    reaches a binding."""
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            read.add(node.id)
        elif isinstance(node, ast.Global):
            read.update(node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            read.add(node.value)
    return read


def orphaned_added_names(base: str, sides: list[str], merged: str) -> list[str]:
    """The module-level names one of SIDES added AND read, that MERGED still
    defines and never reads.

    Both conditions on the parent are load-bearing. `added` alone reports a name
    written for another module to import. `read by that parent` narrows it to a
    name whose own module used it, so a merged file that keeps the definition
    and reads it nowhere has lost every use of what it kept — which is the
    merge's doing, not the branch's."""
    base_tree, merged_tree = _parse(base), _parse(merged)
    if base_tree is None or merged_tree is None:
        return []
    base_names = module_level_identifiers(base_tree)
    orphaned = module_level_identifiers(merged_tree) - names_read(merged_tree)
    found: set[str] = set()
    for side in sides:
        side_tree = _parse(side)
        if side_tree is None:
            continue
        added = module_level_identifiers(side_tree) - base_names
        found |= added & orphaned & names_read(side_tree)
    return sorted(found)


def _resurrectable(text: str) -> set[str]:
    """The lines of TEXT whose reappearance in a merge would mean something.

    Stripped, so an indentation change does not hide one. A line has to be long
    enough to be distinctive, carry a word, not be a comment, and appear exactly
    once — a line the file already repeats says nothing about which copy came
    back."""
    seen = Counter(line.strip() for line in text.splitlines())
    return {
        line
        for line, count in seen.items()
        if count == 1
        and len(line) >= _MIN_RESURRECTED_LINE
        and not line.startswith("#")
        and any(char.isalnum() for char in line)
    }


def resurrected_line_numbers(base: str, sides: list[str], merged: str) -> list[int]:
    """The 1-based MERGED line numbers of every line BASE holds, that NO side of
    the merge still holds anywhere, and that MERGED brings back.

    Absence from every side is what makes this a resurrection rather than an
    ordinary merge: a line one side still carries traces to that side, and a
    line one side merely MOVED is still somewhere in its blob.

    Line NUMBERS, never the text: a source line is arbitrary bytes, and `land`
    splices what this returns into a privileged pull-request comment."""
    gone = _resurrectable(base)
    for side in sides:
        gone -= {line.strip() for line in side.splitlines()}
    return sorted(
        number
        for number, line in enumerate(merged.splitlines(), start=1)
        if line.strip() in gone
    )


def _masked(line: str, *, literals: bool = True) -> str | None:
    """LINE with its COMMENT dropped, and its STRING and FSTRING tokens replaced
    when LITERALS.

    A `not` after a `#` is prose and a `not` inside `run("do not delete")` is
    text; both read as negations without this. The VERDICT masks literals, so a
    pair differing only inside one still differs. The KEY does not, so two lines
    stating different literals key apart. None when the line does not tokenize
    on its own — a continuation, or half an open bracket — because a partial
    parse names the wrong tokens."""
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
        is_literal = name == "STRING" or name.startswith("FSTRING")
        out.append(_MASKED if literals and is_literal else token.string)
        column = token.end[1]
    return "".join(out)


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
    differing only in spacing shares the polarity-free form with no negation
    between them, and states one thing twice."""
    return _rewritten(line)[1]


def _negations_differ(ours: str, theirs: str) -> bool:
    """Whether OURS and THEIRS carry a different number of negations, counted
    over code alone."""
    ours_code, theirs_code = _masked(ours), _masked(theirs)
    if ours_code is None or theirs_code is None:
        return False
    return negation_marks(ours_code) != negation_marks(theirs_code)


def _flat(line: str) -> str:
    return " ".join(line.split())


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _carries_a_statement(stripped: str) -> bool:
    """Whether STRIPPED holds executable syntax for a negation to turn over. A
    bare `)` holds none, and a lone block keyword states nothing.

    This BOUNDS the pair search below rather than deciding it. Comments and
    closing brackets are the bulk of a large diff's added lines, they all key
    alike, and the loop over one key is quadratic in what lands there."""
    return stripped not in _STRUCTURE_ONLY and bool(_IDENTIFIER.search(stripped))


def _keyed(added: set[str]) -> dict[tuple[str, str], set[str]]:
    """The added lines that can carry a contradiction, keyed by indentation and
    polarity-free text.

    KEYED with the literals left in, so two lines stating different things key
    apart: `assert "x" in deny` and `assert "y" not in deny` are consistent, and
    a literal-masked key pairs them. The comment is dropped either way, so a
    trailing one does not split a pair. `_negations_differ` is what decides a
    key's candidates, so a line admitted here is a candidate, not a finding."""
    keyed: dict[tuple[str, str], set[str]] = {}
    for line in added:
        code = _masked(line)
        if code is None or not _carries_a_statement(code.strip()):
            continue
        keyed.setdefault(
            (_indent(line), polarity_free(_masked(line, literals=False))), set()
        ).add(line.rstrip("\n"))
    return keyed


def _seam(merged: list[str], ours: str, theirs: str) -> set[int]:
    """The line numbers OURS and THEIRS occupy in MERGED, when both are there
    and close enough together to read as one seam. Empty otherwise.

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


def contradicting_line_numbers(
    head_added: set[str], base_added: set[str], merged_text: str
) -> list[int]:
    """The 1-based MERGED_TEXT line numbers of every contradicting pair.

    A pair counts only when BOTH of its lines survived into the merged text: one
    side's addition alone is a resolution that chose, which is the answer this
    check wants.

    A line BOTH parents added is dropped first. One parent that added a whole
    block asserting a thing and then its negation — a cherry-pick, or a rename
    read as a new file — carries the pair on its own, and the merge creates
    nothing."""
    only_head, only_base = head_added - base_added, base_added - head_added
    head_keyed, base_keyed = _keyed(only_head), _keyed(only_base)
    merged = [line.rstrip("\n") for line in merged_text.splitlines()]
    numbers: set[int] = set()
    for key in head_keyed.keys() & base_keyed.keys():
        for ours in head_keyed[key]:
            for theirs in base_keyed[key]:
                if _negations_differ(ours, theirs):
                    numbers.update(_seam(merged, ours, theirs))
    return sorted(numbers)


def added_lines(base: str, side: str) -> dict[str, set[str]]:
    """PATH -> every line SIDE added to it since BASE.

    `--unified=0` so the output carries no context to mistake for an addition,
    and `--find-renames` so a renamed file reads as a rename rather than as a
    new file whose whole inherited body is an addition. `--no-renames` produces
    that reading, and it makes a pair the ancestor already carried look like one
    this merge created.

    The `+++` header is told from a hunk line by POSITION, not by its text: a
    file whose own content holds `++ x` prints `+++ x` inside a hunk, and
    reading that as a header files the rest of the file's additions under a path
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


def describe_names(names: list[str]) -> str:
    """NAMES as the list `land` renders, truncated with a count so one mangled
    resolution cannot fill a pull-request comment."""
    shown = ", ".join(names[:_NAMES_SHOWN])
    rest = len(names) - _NAMES_SHOWN
    return f"{shown}, and {rest} more" if rest > 0 else shown


class ContradictionReport:
    """The APPLICATION of the checks above to one bundle step.

    A mixin for the reason `NeitherSideReport` is one: every method reads the
    step's own resolved set and the two parents it merged."""

    def _blob(self, sha: str, name: str) -> str | None:
        """NAME's content at SHA, or None when SHA does not hold it.

        An absent path is the ordinary answer — one side adds a file, or the
        merge base predates it — so this reads the exit status."""
        done = git_result("show", f"{sha}:{name}")
        return done.stdout if done.returncode == 0 else None

    def _gated_python_paths(self) -> list[str]:
        """The resolved paths this check judges.

        Deferred, modify/delete and declined paths are excluded on the grounds
        `report_lines_from_neither_side` excludes them: the resolution did not
        author their content. A path absent from the worktree was deleted."""
        gated = (
            set(self.allowed)
            - set(self.deferred)
            - set(self.modify_delete)
            - set(self.declined)
        )
        return sorted(
            name for name in gated if name.endswith(".py") and Path(name).is_file()
        )

    def report_a_contradictory_merge(self) -> None:
        """Name every place the surviving lines contradict each other, and hand
        the list to `land` so auto-merge goes off.

        Run over the tree as it will be COMMITTED, after the hooks and the
        post-merge repair pass, for the reason `report_lines_from_neither_side`
        runs there: both rewrite files and move every line below them."""
        paths = self._gated_python_paths()
        if not paths:
            return
        merge_base = git(
            "merge-base", self.checked_out_head, self.merge_base_side
        ).strip()
        head_added = added_lines(merge_base, self.checked_out_head)
        base_added = added_lines(merge_base, self.merge_base_side)
        # Capped over the paths this loop READS, not every path both parents
        # touched: a cap counting paths it never examines drops a gated one to
        # make room for a path outside the resolution.
        both_added = set(paths) & set(head_added) & set(base_added)
        if len(both_added) > _MAX_PATHS:
            print(
                f"::warning::both parents added lines to {len(both_added)} of the "
                f"resolved paths; the contradicting-union check read the first "
                f"{_MAX_PATHS}."
            )
            both_added = set(sorted(both_added)[:_MAX_PATHS])
        for name in paths:
            try:
                merged = Path(name).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # This check reports and never refuses, so it must not be what
                # kills a resolution. A `.py` file that is not UTF-8 is one it
                # has nothing to say about.
                print(f"::warning::'{name}' is not UTF-8; read no contradiction in it.")
                continue
            base = self._blob(merge_base, name)
            sides = [
                blob
                for blob in (
                    self._blob(self.checked_out_head, name),
                    self._blob(self.merge_base_side, name),
                )
                if blob is not None
            ]
            # A path one side ADDED has no base blob to have dropped anything
            # from, and a path with fewer than two sides is not a two-sided
            # resolution at all. The union check reads the parents' diffs
            # instead, so it stands on its own.
            if base is not None and len(sides) == 2:
                self._claim(
                    name, "orphaned-binding", orphaned_added_names(base, sides, merged)
                )
                self._claim(
                    name,
                    "resurrected-line",
                    resurrected_line_numbers(base, sides, merged),
                )
            if name in both_added:
                self._claim(
                    name,
                    "contradicting-union",
                    contradicting_line_numbers(
                        head_added[name], base_added[name], merged
                    ),
                )

    def _claim(self, name: str, kind: str, found: list[str] | list[int]) -> None:
        """Record one finding for `land`, and say it in the job log.

        One sidecar for the three kinds, so `land` parses one record shape and a
        hardening fix lands once. A name list and a line-number list render
        differently, which is what KIND selects."""
        if not found:
            return
        detail = (
            describe_names(found) if kind == "orphaned-binding" else describe(found)
        )
        self.contradiction_findings.append(f"{name}\t{kind}\t{detail}")
        print(
            f"::warning::{_SAID[kind].format(detail=detail, name=name)} Every line "
            "traces to a parent, so no conflict and no delta review names this: "
            "the merge is landing with auto-merge off."
        )
