"""PROBLEM CLASS — the merge is broken and every one of its lines traces to a
parent, so no provenance check and no delta review names it.

`_out_of_conflict` and `_neither_side` ask where each line came from. A merge
can answer that for every line and still be wrong, because the lines that
SURVIVED contradict each other. Each shape below reaches this from a real
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
* a call kept past its definition. One parent renamed a shell helper and
  deleted it, the other added a call to it; the merge took both, so the call
  exits 127 inside an `if` and the branch takes its `else` arm in silence
  (this repository's #149). `_undefined_command` owns this one.
* a definition kept past its call. One parent added a shell helper and called
  it, the other inlined that work; the merge kept the helper and dropped its
  only call, so nothing runs it (agent-glovebox#6400, #6144).
  `_undefined_command` owns this one too.
* a definition dropped from one file while a file sourcing it keeps calling it.
  The caller merged clean, so no conflict pointed at the break
  (agent-glovebox#6940). `_undefined_command` owns this one as well.
* one parent's file taken whole, while the other parent changed that same file
  since the merge base. Every line traces to the kept parent, so the drop is
  invisible, and no later merge of the base surfaces it either
  (agent-glovebox#5866). `_taken_whole` owns the predicate.
* one definition kept twice. Both parents added the same import, constant or
  function, and the merge kept both copies. Python and bash run the last one,
  so the first is dead, and a test defined twice never runs (agent-glovebox
  c80ad67d23 kept `import json` twice, 4780060bc7 a bash helper twice).

Read through a real grammar or not at all — `ast` for Python, tree-sitter for
shell — matching `dropped_name_seams.py`'s contract: a language with no parser
here is out of scope, never a guess. A count over line TEXT is out of scope for the same
reason, so the duplicate check counts the top-level DEFINITIONS a parser reads.
Every check compares the merge with both parents, so a finding names a line the
MERGE produced rather than one a branch carried.

Reported, never refused, for the reason `_neither_side` reports. Each check
below is a heuristic with tuned precision filters, so a refusal on a false
positive throws away a resolution the model was already billed for and hands a
human the raw conflict as well. `land` names the findings and turns auto-merge
off, and the pull request's own checks read exactly this tree.
"""

import ast
import io
import os
import re
import sys
import tempfile
import time
import tokenize
from collections import Counter
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    git,
    git_bytes,
)
from _neither_side import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    describe,
)
from dropped_name_seams import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    module_level_identifiers,
)
from _undefined_command import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    CONSTANT_NAME,
    MAX_PATHS as _MAX_SHELL_PATHS,
    dropped_definition_seams,
    is_shell,
    orphaned_definitions,
    shell_seams,
    top_level_definitions,
)
from _post_merge_check import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    REPAIR_FLOOR_SECONDS,
    run as run_post_merge_check,
)
from _pre_pass import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    untrusted_head,
)
from _taken_whole import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    TakenWhole,
    taken_whole,
)
from prompts import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    CONTRADICTION_REJECTED,
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
# merge of two long branches can reach thousands, and the pair search over one
# path is quadratic in the lines both parents added to it.
_MAX_PATHS = 200
# Names one finding quotes, matching `_neither_side`'s range count.
_NAMES_SHOWN = 5
# Records handed to `land`, matching `dropped_name_seams`'s own total. Each one
# renders a bullet into a pull-request comment that already carries a dozen
# other notes, and a mangled resolution can produce three per path.
_TOTAL_CAP = 40

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
# A name `land`'s record grammar can carry. Python 3 identifiers may hold any
# word character, and the grammar is ASCII, so a name outside this is counted
# rather than spelled — a record it rejects loses the path it names.
_SPELLABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
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
    "undefined-command": (
        "'{name}' calls {detail}, which a parent of it defined and the merge "
        "does not — in bash that exits 127 inside an `if` and says nothing."
    ),
    "orphaned-definition": (
        "the resolution left the bash function(s) {detail} in '{name}' defined "
        "and called nowhere in the merged tree, and a parent that added one of "
        "them did call it."
    ),
    "dropped-definition": (
        "the merge dropped the bash function(s) {detail} from '{name}', a "
        "parent of it defined them, and a file that sources '{name}' still "
        "calls them — in bash that exits 127 inside an `if` and says nothing."
    ),
    "taken-whole": (
        "the merge carries one parent's whole '{name}' ({detail}), and the "
        "dropped parent changed that same file since the base."
    ),
    "duplicate-definition": (
        "'{name}' defines {detail} more times than either parent does, so a "
        "later copy replaces an earlier one."
    ),
}
# Kinds whose detail is a list of NAMES rather than of line numbers. The two
# render differently, and `land` parses each against its own grammar.
_NAME_KINDS = frozenset(
    {
        "orphaned-binding",
        "undefined-command",
        "orphaned-definition",
        "dropped-definition",
        "duplicate-definition",
    }
)


def _parse(text: str | None) -> ast.Module | None:
    """TEXT as a module, or None when it is absent or does not parse.

    An unparseable side is the ordinary case, not an error: it still carries
    conflict markers, or it is a file mid-port. This analysis has nothing to say
    about one, and a half-parsed comparison misattributes what it finds."""
    if text is None:
        return None
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        # ValueError: a NUL byte, which `ast.parse` refuses before it parses.
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


def _negations_differ(ours: str, theirs: str) -> bool:
    """Whether OURS and THEIRS carry OPPOSITE polarity, counted over code alone.

    Parity, not the raw count: two negations cancel, so `assert not x != y` and
    `assert x == y` say the same thing with counts of 2 and 0. A pair differing
    only in SPACING shares the polarity-free form with equal parity, so this is
    also what tells a contradiction from one statement written twice."""
    ours_code, theirs_code = _masked(ours), _masked(theirs)
    if ours_code is None or theirs_code is None:
        return False
    return _rewritten(ours_code)[1] % 2 != _rewritten(theirs_code)[1] % 2


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
            (_indent(line), _rewritten(_masked(line, literals=False))[0]), set()
        ).add(line)
    return keyed


def _bodies(tree: ast.Module | None) -> dict[int, int]:
    """Line number -> the line the innermost `def` or `class` holding it starts
    on, for every line one holds. Empty when TREE is None.

    Ascending by start line, so a nested definition overwrites the outer one it
    sits inside and each line ends up under its innermost holder."""
    if tree is None:
        return {}
    holders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    ]
    holders.sort(key=lambda node: node.lineno)
    return {
        line: node.lineno
        for node in holders
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }


def _seam(
    merged: list[str], bodies: dict[int, int], ours: str, theirs: str
) -> set[int]:
    """The line numbers OURS and THEIRS occupy in MERGED, when both are there
    and read as one seam. Empty otherwise.

    One seam means the same innermost `def` or `class` AND within `_SEAM_LINES`.
    The enclosure is what the line distance alone gets wrong: two adjacent
    four-line test functions, one asserting what the other denies, sit three
    lines apart and are a correct resolution.

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
        if abs(one - other) <= _SEAM_LINES and bodies.get(one) == bodies.get(other)
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
    merged = merged_text.splitlines()
    bodies = _bodies(_parse(merged_text))
    numbers: set[int] = set()
    for key in head_keyed.keys() & base_keyed.keys():
        for ours in head_keyed[key]:
            for theirs in base_keyed[key]:
                if _negations_differ(ours, theirs):
                    numbers.update(_seam(merged, bodies, ours, theirs))
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
    # Read as bytes and decoded leniently. This is a WHOLE-TREE diff, so it
    # carries content from every file in the range, and one Latin-1 `.po` that
    # git does not call binary makes a strict decode raise — which would make
    # this check the thing that kills a resolution. A byte that does not decode
    # cannot equal a line of a `.py` file, so replacing it costs no finding.
    raw = git_bytes(
        "-c",
        "core.quotePath=false",
        "diff",
        "--unified=0",
        "--no-color",
        "--find-renames",
        f"{base}..{side}",
    )
    if raw is None:
        return added
    for line in raw.decode("utf-8", errors="replace").splitlines():
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


def python_definitions(text: str | None) -> Counter[str] | None:
    """NAME -> how many module-level statements of TEXT bind it: a def or class,
    an import, or a `CONSTANT_NAME` assignment. None when TEXT does not parse.
    `@overload` stubs and `_` are defined many times on purpose."""
    tree = _parse(text)
    if tree is None:
        return None
    found: Counter[str] = Counter()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            overload = any(
                getattr(d, "id", getattr(d, "attr", None)) == "overload"
                for d in node.decorator_list
            )
            if node.name != "_" and not overload:
                found[node.name] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # A dotted import keys on its whole path: `import a.b` and `import a.c`
            # both bind `a`, and each is still live.
            found.update(a.asname or a.name for a in node.names if a.name != "*")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            # A value that reads the name rebuilds it (`X = X | {...}`).
            read = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            found.update(
                t.id
                for t in targets
                if isinstance(t, ast.Name)
                and CONSTANT_NAME.fullmatch(t.id)
                and t.id not in read
            )
    return found


def duplicated_names(
    sides: list[Counter[str] | None], merged: Counter[str] | None
) -> list[str]:
    """Every name MERGED defines twice or more, and more often than either side
    does. A parent that already held the copies shipped them, so they are not
    this merge's. A version no parser read declines the comparison."""
    if merged is None or any(side is None for side in sides):
        return []
    return sorted(
        name
        for name, count in merged.items()
        if count >= 2 and count > max(side[name] for side in sides)
    )


def describe_names(names: list[str]) -> str:
    """NAMES as the list `land` renders, truncated with a count so one mangled
    resolution cannot fill a pull-request comment.

    A name `land`'s grammar cannot carry is counted rather than spelled. It would
    otherwise fail that grammar, and the record then names no file at all."""
    spellable = [name for name in names if _SPELLABLE.match(name)]
    rest = len(names) - min(len(spellable), _NAMES_SHOWN)
    if not spellable:
        return f"{rest} name(s) this report cannot spell"
    shown = ", ".join(spellable[:_NAMES_SHOWN])
    return f"{shown}, and {rest} more" if rest > 0 else shown


class ContradictionReport:
    """The APPLICATION of the checks above to one bundle step.

    A mixin for the reason `NeitherSideReport` is one: every method reads the
    step's own resolved set and the two parents it merged."""

    def _blob(self, sha: str, name: str) -> str | None:
        """NAME's UTF-8 content at SHA, or None when SHA does not hold it or the
        blob is not UTF-8.

        An absent path is the ordinary answer — one side adds a file, or the
        merge base predates it. A blob that does not decode is one these checks
        have nothing to say about, and raising on it would make this check the
        thing that kills a resolution."""
        raw = git_bytes("show", f"{sha}:{name}")
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _gated_paths(self, wanted: Callable[[str], bool]) -> list[str]:
        """The resolved paths this check judges, of those WANTED accepts.

        `gated_paths` is the shared exclusion set every content check reads. A
        re-derived generated region is excluded on top of it, and only here: both
        parents edit the region, so neither blob still holds a line the generator
        re-emits after the merge, and that reads as a resurrection. A path absent
        from the worktree was deleted."""
        gated = self.gated_paths() - set(self.rederived_regions)
        return sorted(name for name in gated if wanted(name) and Path(name).is_file())

    def report_a_contradictory_merge(self) -> None:
        """Name every place the surviving lines contradict each other, and hand
        the list to `land` so auto-merge goes off.

        Run over the tree as it will be COMMITTED, after the hooks and the
        post-merge repair pass, for the reason `report_lines_from_neither_side`
        runs there: both rewrite files and move every line below them."""
        # `_cap_the_findings` truncates the tail, so order is priority. The shell
        # loop can emit three records for each of 60 paths, enough to fill the
        # cap alone, so it follows the Python checks. A duplicate copy is the
        # least of these findings, so it comes last.
        self._report_python_contradictions()
        self._report_taken_whole_files()
        self._report_undefined_commands()
        self._report_duplicate_definitions()
        self._cap_the_findings()

    def one_sided_takes(self) -> dict[str, TakenWhole]:
        """Every resolved path the merge carries one parent whole for, while the
        other parent changed that same file since the merge base.

        Deterministic and model-free — `ls-tree` over the index and the two
        parents — so the post-merge budget's owner can ask this BEFORE it decides
        what to reserve, and pay nothing for the answer. Reads nothing and
        records nothing, which is what lets it run twice in one step.

        Read from the INDEX, which is what `commit_the_merge` writes: `write-tree`
        turns it into a tree the parent comparison reads with `ls-tree`. An index
        holding unmerged entries has no such tree, and the step refuses that state
        before this runs."""
        merged_tree = git("write-tree", check=False).strip()
        if not merged_tree:
            print(
                "::warning::the index holds no tree to compare against the "
                "parents, so no path was read for a one-sided take."
            )
            return {}
        parents = [merged_tree, self.checked_out_head, self.merge_base_side]
        # Capped like its two sibling arms, and for a sharper reason: `taken_whole`
        # spends up to four `ls-tree` calls per path, inside a step that carries a
        # wall-clock budget.
        paths = self._gated_paths(lambda _name: True)
        if len(paths) > _MAX_PATHS:
            print(
                f"::warning::the resolution touched {len(paths)} paths; the "
                f"taken-whole check read the first {_MAX_PATHS}."
            )
            paths = paths[:_MAX_PATHS]
        return taken_whole(parents, paths)

    def _report_taken_whole_files(self) -> None:
        """Keep every one-sided take for `land`, and say each one in the job log."""
        self.taken_whole_takes = self.one_sided_takes()
        for name, take in sorted(self.taken_whole_takes.items()):
            self._record(
                name,
                "taken-whole",
                f"kept {take.kept}, dropped {take.dropped}, base {take.base}",
            )

    def repair_contradictions_once(self) -> None:
        """The run's ONE bounded model pass over a merge whose surviving lines
        contradict each other, then every check above again over what it wrote.

        A finding that survives rides `land`'s comment and turns auto-merge off,
        exactly as one does when no pass runs at all: this adds no second report
        surface, it only gives the resolution a chance to lose the finding."""
        if self.contradiction_repair_spent or not self.contradiction_findings:
            return
        self.contradiction_repair_spent = True
        # HALF of what the post-merge clock has left, because the re-check below
        # runs the same command over the same tree and needs the other half. Under
        # the floor, a pass would rewrite the merged tree and leave nothing able to
        # read the rewrite, so no pass runs and the finding above stands.
        budget = (self.post_merge_deadline() - time.monotonic()) / 2
        if budget < REPAIR_FLOOR_SECONDS:
            print(
                "::notice::no repair pass over this contradictory merge: the "
                f"post-merge budget leaves {max(budget, 0.0):.0f}s for one once its "
                f"re-check is reserved, and a pass needs {REPAIR_FLOOR_SECONDS:.0f}s."
            )
            return
        # PUT BACK, never refused: this check reports and never kills a
        # resolution, so a repair the content gates reject leaves the tree as it
        # was and the finding below stands exactly as it did.
        if not self.repair_or_put_back(
            self._contradiction_report(), CONTRADICTION_REJECTED, budget
        ):
            return
        # The pass rewrote the merged tree, so every reader indexed to that tree
        # reads it again. The caller's check judged bytes the pass has replaced,
        # and a carried-forward report names lines the commit below no longer holds.
        self.post_merge_finding = run_post_merge_check(
            untrusted_head=untrusted_head(),
            repair=self.repair_post_merge_once,
            head_sha=self.checked_out_head,
            base_sha=self.merge_base_side,
            deadline=self.post_merge_deadline(),
            prior=self.post_merge_finding,
        )
        self.neither_side_lines = []
        self.report_lines_from_neither_side()
        self.contradiction_findings = []
        self.report_a_contradictory_merge()

    def _contradiction_report(self) -> Path:
        """What the repair pass is asked to fix, on disk.

        A taken-whole finding carries the DROPPED side's own diff for that path.
        The merged file holds the kept parent's exact bytes, so nothing in the
        tree says what the other side changed there, and the pass has nothing to
        reconcile without it."""
        blocks: list[str] = []
        for record in self.contradiction_findings:
            name, kind, detail = record.split("\t", 2)
            blocks.append(_SAID[kind].format(detail=detail, name=name))
            take = self.taken_whole_takes.get(name) if kind == "taken-whole" else None
            if take is not None:
                blocks.append(
                    f"What {take.dropped} changed in {name} since {take.base}:\n"
                    + git("diff", take.base, take.dropped, "--", name, check=False)
                )
        handle, path = tempfile.mkstemp()
        os.close(handle)
        report = Path(path)
        report.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
        return report

    def _single_merge_base(self) -> str:
        """The one base a check measures a side's additions from, or "".

        A criss-cross history has several equally good bases, and git merges
        those into a virtual ancestor no single sha names. Reading one
        arbitrarily would attribute a change inherited from another as newly
        added, so every check that asks "since the base" declines there."""
        bases = git("merge-base", "--all", self.checked_out_head, self.merge_base_side)
        return bases.strip() if len(bases.split()) == 1 else ""

    def _report_python_contradictions(self) -> None:
        """The three shapes `ast` reads, over the resolution's Python paths."""
        merge_base = self._single_merge_base()
        if not merge_base:
            print(
                "::warning::the parents have several merge bases, so the "
                "contradictory-merge check read no Python in this resolution."
            )
            return
        paths = self._gated_paths(lambda name: name.endswith(".py"))
        if not paths:
            return
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

    def _report_duplicate_definitions(self) -> None:
        """Name every top-level definition the merge holds more copies of than
        either parent, over the resolution's Python and shell paths.

        It reads the two parents and the merge alone, never the base, so it
        stands on a criss-cross history the other checks decline."""
        paths = self._gated_paths(lambda name: name.endswith(".py") or is_shell(name))
        if len(paths) > _MAX_PATHS:
            print(
                f"::warning::the resolution touched {len(paths)} Python and shell "
                f"files; the duplicate-definition check read the first {_MAX_PATHS}."
            )
            paths = paths[:_MAX_PATHS]
        for name in paths:
            # A symlink's blob is its target's path, not the text the tree reads.
            if Path(name).is_symlink():
                continue
            try:
                merged = Path(name).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                print(f"::warning::'{name}' is not UTF-8; read no definition in it.")
                continue
            sides = [
                self._blob(sha, name)
                for sha in (self.checked_out_head, self.merge_base_side)
            ]
            # A file one side ADDED holds only its author's copies, not a merge's.
            if None in sides:
                continue
            count = (
                python_definitions if name.endswith(".py") else top_level_definitions
            )
            self._claim(
                name,
                "duplicate-definition",
                duplicated_names([count(side) for side in sides], count(merged)),
            )

    def _report_undefined_commands(self) -> None:
        """Name every shell call this resolution left with no definition, every
        definition it left with no call, and every definition it dropped that a
        file sourcing that path still calls.

        Its own loop rather than an arm of the Python one: it reads a different
        parser and a different suffix, and it carries its own cap because each
        path here spends up to five `git grep`s over the merged tree."""
        paths = self._gated_paths(is_shell)
        if not paths:
            return
        if len(paths) > _MAX_SHELL_PATHS:
            print(
                f"::warning::the resolution touched {len(paths)} shell files; the "
                f"undefined-command check read the first {_MAX_SHELL_PATHS}."
            )
            paths = paths[:_MAX_SHELL_PATHS]
        # `undefined_calls` reads the two parent blobs alone, so it stands
        # whatever the history looks like. The orphan arm asks what a side ADDED
        # since the base, so it alone declines a criss-cross history.
        merge_base = self._single_merge_base()
        if not merge_base:
            print(
                "::warning::the parents have several merge bases, so no shell "
                "definition was judged orphaned in this resolution."
            )
        for name in paths:
            try:
                merged = Path(name).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # Reported and never refused, as the Python loop above is: a
                # shell file that is not UTF-8 is one this has nothing to say
                # about, not a reason to kill a paid resolution.
                print(f"::warning::'{name}' is not UTF-8; read no shell call in it.")
                continue
            sides = [
                blob
                for blob in (
                    self._blob(self.checked_out_head, name),
                    self._blob(self.merge_base_side, name),
                )
                if blob is not None
            ]
            # Both parents, or there is no two-sided resolution to blame: a file
            # one side ADDED carries its own author's call, not a merge's.
            if len(sides) != 2:
                continue
            self._claim(name, "undefined-command", shell_seams(sides, merged, name))
            self._claim(
                name,
                "dropped-definition",
                dropped_definition_seams(sides, merged, name),
            )
            base = self._blob(merge_base, name) if merge_base else None
            # A path one side ADDED has no base blob, so nothing there was added
            # SINCE one and the orphan question does not arise.
            if base is not None:
                self._claim(
                    name,
                    "orphaned-definition",
                    orphaned_definitions(base, sides, merged, name),
                )

    def _cap_the_findings(self) -> None:
        """Bound what `land` renders into the pull-request comment.

        `describe_names` and `describe` bound the inside of ONE record. Nothing
        bounds their NUMBER, and a mangled resolution produces up to three per
        path, so a wide one would render a body `gh` rejects."""
        if len(self.contradiction_findings) <= _TOTAL_CAP:
            return
        print(
            f"::warning::the contradictory-merge check found "
            f"{len(self.contradiction_findings)} things; the pull request names "
            f"the first {_TOTAL_CAP}."
        )
        self.contradiction_findings = self.contradiction_findings[:_TOTAL_CAP]

    def _claim(self, name: str, kind: str, found: list[str] | list[int]) -> None:
        """Record one finding for `land` over a LIST this kind found.

        A name list and a line-number list render differently, which is what
        KIND selects."""
        if not found:
            return
        detail = describe_names(found) if kind in _NAME_KINDS else describe(found)
        self._record(name, kind, detail)

    def _record(self, name: str, kind: str, detail: str) -> None:
        """Keep one finding for `land`, and say it in the job log.

        One sidecar for every kind, so `land` parses one record shape and a
        hardening fix lands once."""
        self.contradiction_findings.append(f"{name}\t{kind}\t{detail}")
        print(
            f"::warning::{_SAID[kind].format(detail=detail, name=name)} Every line "
            "traces to a parent, so no conflict and no delta review names this: "
            "the merge is landing with auto-merge off."
        )
