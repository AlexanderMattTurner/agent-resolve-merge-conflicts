"""Auto-resolve — UNDEFINED-COMMAND check (shell).

PROBLEM CLASS — the merge kept a call to a shell function the other parent
deleted. Each parent is right on its own: one replaced the helper and dropped
it, the other added a call to it. Every merged line traces to a parent, so
`_out_of_conflict`, `_neither_side` and the delta review all pass the merge
(this repository's #149: `prepare.sh` kept `is_modify_delete` after the base
branch replaced it with `has_fact` and deleted the helper).

Bash is why the break is silent rather than loud. `if` suspends `errexit`, so
the missing command exits 127, the branch takes its `else` arm, and the whole
feature becomes a no-op. #149's only symptom was one test reading `''`.

A finding must be provably MERGE-CAUSED. `undefined_calls` proves it inside one
file: the merged file calls the name, a parent's version of that same file
defines it, and the merged file does not. No check here consults a `PATH`
oracle, which would answer about the runner's image rather than the repository.

`orphaned_definitions` reads the OPPOSITE direction: a function one parent added
and called, which the merge kept while dropping every call to it
(agent-glovebox#6400, #6144). `dropped_definition_seams` reads the loss ACROSS
files, where the surviving call sits in a script nobody resolved, and its own
docstring states the weaker condition it settles for (agent-glovebox#6940). One
module, because the three questions share every bash reader below.
"""

import functools
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Shell paths this check reads per resolution. Parsing is cheap, but each path
# spends up to five tree searches below, so a resolution touching hundreds of
# scripts would spend the step's whole budget here.
MAX_PATHS = 60
# Files ONE search parses. The patterns below are definition- and call-shaped,
# so a name reaching this many candidates is a name written all over the tree.
# The truncation is reported rather than silent in BOTH directions: a dropped
# candidate is either the file that suppresses a finding or the one that
# establishes it.
_MAX_RELOCATION_FILES = 200
# Shell function names a search below can look for: `_FUNCTION_NAME`'s charset,
# anchored, with `_ere_literal` escaping what POSIX ERE would otherwise read as
# an operator. A name outside it is reported rather than searched where the
# search SUPPRESSES a finding, and reported unsearched where it establishes one
# — either way the check never goes silent over a name it cannot spell.
_SEARCHABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:@+-]*\Z")
# The two characters `_SEARCHABLE`'s charset shares with POSIX ERE's operators.
# `:`, `@`, `-` and `_` are literal outside a bracket expression, so escaping
# them would be the mis-escape the pre-filter must avoid.
_ERE_OPERATORS = re.compile(r"[.+]")
_SHELL_SUFFIXES = (".sh", ".bash")
# What bash spells "run this file in my own shell", which is the only way one
# file's function reaches another.
_SOURCE_COMMANDS = frozenset({"source", "."})
# A path tail a source target names statically. An expansion in the last
# component (`source "$lib"`) leaves nothing to compare, and a leading `.` or
# `-` would read as a flag rather than a file. It opens on `_SEARCHABLE`'s own
# first class, so a basename this accepts is always one a search can look for.
_SOURCE_BASENAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
# What a shell script with no suffix opens with. `_gated_paths` has already cut
# the set to this resolution's own files, so reading one line of each is cheap
# — and `.hooks/pre-commit` is exactly where a silent 127 does the most damage.
_SHELL_SHEBANG = re.compile(rb"^#![^\n]*\b(ba|da|k|z)?sh\b")


def warn(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@functools.lru_cache(maxsize=1)
def _reader():
    """`lib_bash_ast`, or None when tree-sitter is not installed.

    Imported here rather than at module scope. `install-hook-tools.sh` pins this
    parser itself and asserts its import, so a same-repository resolve always has
    it. That step is SKIPPED on a fork head, where `bundle.py` still imports this
    module, and the merge-delta report imports it under a caller job's own
    python3. Crashing there would discard a resolution the model was already
    billed for, so each reader stands down and says so in the job log instead.
    """
    try:
        import lib_bash_ast  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        warn(
            f"::warning::bash-parser: tree-sitter is not installed ({exc}), so no "
            "shell file is read in this job."
        )
        return None
    return lib_bash_ast


def _root(text: str):
    """TEXT's bash tree, or None when no parser read all of it.

    `parse_clean` declines a tree with an ERROR region, and this comparison is
    why: the grammar recovers by wrapping the bad region and parsing on, so a
    definition inside the hole disappears while the calls around it survive —
    which reads exactly like the break this check exists to name."""
    reader = _reader()
    return None if reader is None else reader.parse_clean(text)


# A name the merge-delta note can quote verbatim. The grammar accepts a quoted
# word after `function`, so a name node can hold a backtick or a newline, which
# would end the note's code span; such a definition is not counted.
_FUNCTION_NAME = re.compile(r"[A-Za-z0-9_.:@+-]+")
# A variable its author means to set once, in either language. A lowercase one
# is state a script reassigns on purpose, so defining it twice is no evidence.
CONSTANT_NAME = re.compile(r"_*[A-Z][A-Z0-9_]*")


def function_sources(text: str) -> dict[str, list[str]] | None:
    """NAME -> the bytes of each function definition in TEXT binding it, or
    None when no parser read all of TEXT. A definition inside a body, a list
    or a redirection still binds when it runs, so every one counts."""
    root = _root(text)
    if root is None:
        return None
    out: dict[str, list[str]] = {}
    for node in _reader().walk(root):
        if node.type != "function_definition":
            continue
        name = node.child_by_field_name("name")
        if name is None:
            continue
        spelled = name.text.decode()
        if _FUNCTION_NAME.fullmatch(spelled) is None:
            continue
        out.setdefault(spelled, []).append(node.text.decode())
    return out


def top_level_definitions(text: str) -> Counter[str] | None:
    """NAME -> how many top-level statements of TEXT define it: a function, or a
    `CONSTANT_NAME` variable. None when no parser read all of TEXT. A definition
    inside an `if` or a body binds on one path only, so it is not counted."""
    root = _root(text)
    if root is None:
        return None
    found: Counter[str] = Counter()
    for node in root.children:
        if node.type in ("function_definition", "variable_assignment"):
            names = [node.child_by_field_name("name")]
        elif node.type == "declaration_command":
            names = [
                child.child_by_field_name("name")
                for child in node.children
                if child.type == "variable_assignment"
            ]
        else:
            continue
        for name in names:
            if name is None:
                continue
            spelled = name.text.decode()
            if node.type == "function_definition" or CONSTANT_NAME.fullmatch(spelled):
                found[spelled] += 1
    return found


def defined_functions(text: str) -> set[str]:
    """Every function TEXT defines, read from the bash grammar."""
    root = _root(text)
    if root is None:
        return set()
    names = set()
    for node in _reader().walk(root):
        if node.type != "function_definition":
            continue
        name = node.child_by_field_name("name")
        if name is not None:
            names.add(name.text.decode())
    return names


def _calls(node) -> set[str]:
    """Every name the subtree at NODE invokes as a command."""
    names = set()
    for child in _reader().walk(node):
        words = _reader().command_words(child)
        if words:
            names.add(words[0])
    return names


def called_names(text: str) -> set[str]:
    """Every name TEXT invokes as a command.

    `command_words` answers None for a name an expansion decides at run time
    (`$helper "$f"`), which this check cannot judge — and does not clear."""
    root = _root(text)
    return set() if root is None else _calls(root)


def _self_wrapping(sides: list[str]) -> set[str]:
    """Names a parent defines as a WRAPPER around the command of the same name.

    `grep() { command grep --color=never "$@"; }` is the shape: dropping the
    definition restores the command, so every surviving call still resolves and
    a finding would cost a correct resolution its auto-merge.

    The body invoking its own name is what proves that command exists, and it
    is read from the merge's own blobs. A list of command names would answer
    about the runner's image instead — the oracle this check refuses — and it
    would answer for the names someone remembered to write down.
    """
    names: set[str] = set()
    for text in sides:
        root = _root(text)
        if root is None:
            continue
        for node in _reader().walk(root):
            if node.type != "function_definition":
                continue
            name = node.child_by_field_name("name")
            if name is not None and name.text.decode() in _calls(node):
                names.add(name.text.decode())
    return names


def _inside_a_function(node) -> bool:
    """Whether NODE sits in a function body, so bash runs it later than the
    surrounding file. A call there reaches a definition written below it."""
    while node is not None:
        if node.type == "function_definition":
            return True
        node = node.parent
    return False


def available_names(text: str) -> set[str]:
    """Every function TEXT defines that TEXT's own top-level calls can reach.

    Bash defines a function when it EXECUTES the definition, so a top-level
    call written above the definition exits 127 exactly as a missing one does.
    Set membership alone would read that file as defining the name and clear a
    break the merge made by REORDERING the two."""
    root = _root(text)
    if root is None:
        return set()
    defined: dict[str, int] = {}
    called: dict[str, int] = {}
    for node in _reader().walk(root):
        if node.type == "function_definition":
            name = node.child_by_field_name("name")
            if name is not None:
                key = name.text.decode()
                defined[key] = min(defined.get(key, node.start_byte), node.start_byte)
            continue
        words = _reader().command_words(node)
        if words and not _inside_a_function(node):
            called.setdefault(words[0], node.start_byte)
    return {
        name
        for name, start in defined.items()
        if name not in called or start < called[name]
    }


def undefined_calls(sides: list[str], merged: str) -> list[str]:
    """Names MERGED calls that a parent's version of the same file reached.

    SIDES are that file's two parent blobs. A name both parents had already
    dropped is not here: the merge did not drop it, so the call it left is a
    break a parent shipped rather than one this resolution made.

    A side no parser could read whole contributes no definitions, which would
    read as a drop, so one unreadable side declines the whole comparison.

    ONE FILE'S call sites is the deliberate bound. A surviving call in another
    file is `dropped_definition_seams`' question."""
    if any(_root(text) is None for text in (merged, *sides)):
        return []
    parents_reach = set().union(*(available_names(side) for side in sides))
    dropped = (parents_reach - available_names(merged)) & called_names(merged)
    return sorted(dropped - _self_wrapping(sides))


# BOTH forms bash accepts, because `defined_functions` reads both: `f()`,
# `f ()`, `function f {`, `function f()`. A parenthesised-only pattern
# shortlists nothing for a helper relocated as `function f {`, so the finding it
# fails to suppress costs a correct resolution its auto-merge. The trailing
# `\(\)|\{` keeps prose out.
_DEFINITION_PATTERN = (
    r"(^|[[:space:]])(function[[:space:]]+)?({alternation})[[:space:]]*(\(\)|\{{)"
)
# A whole-word mention, which is as close as a regex gets to a call: the bash
# parse below is what tells a call from a comment, a string or a definition.
_CALL_PATTERN = r"(^|[^A-Za-z0-9_])({alternation})([^A-Za-z0-9_]|$)"


def _searchable_shell(path: str) -> bool:
    """Whether a `git grep` hit is shell source this check may parse.

    INVARIANT — the same answer `is_shell` gives the resolution's own set, asked
    of the rest of the tree. A tracked script with no suffix, `bin/deploy` with
    a bash shebang, is a caller like any other, and a search that skipped it
    calls a still-called function orphaned. `.hooks` holds git's own hooks,
    which this check reads whatever they open with."""
    return path.startswith(".hooks/") or is_shell(path)


def _ere_literal(name: str) -> str:
    """NAME as a POSIX ERE matching itself.

    `ns.fn` and `f+g` are legal bash names, and unescaped they read as `.` any
    character and `+` one-or-more. The pre-filter would then shortlist the wrong
    files and the parse would clear a name nothing defines."""
    return _ERE_OPERATORS.sub(lambda m: "\\" + m.group(), name)


def _shortlist(names: list[str], exclude: str, pattern: str) -> list[str]:
    """Shell files other than EXCLUDE whose text matches PATTERN for ANY of NAMES.

    One `git grep` for the whole set, never one per name: a search per name
    makes the check's cost quadratic in a mangled resolution, and the parse
    below reads each file's whole name set anyway.

    The grep reads the WHOLE tracked tree and `_searchable_shell` then drops
    every hit that is not shell, because no pathspec names an extensionless
    script. A regex PRE-FILTER, never the answer: it shortlists files for the
    parse, which decides. `git grep` exits 1 on no match, so only a code above
    that is an error."""
    alternation = "|".join(_ere_literal(name) for name in names)
    done = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "-E",
            "-e",
            pattern.format(alternation=alternation),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode > 1:
        warn(
            f"::warning::undefined-command: git grep failed ({done.returncode}) "
            f"looking for {alternation}: {done.stderr.strip()}"
        )
        return []
    return [
        line
        for line in done.stdout.splitlines()
        if line and line != exclude and _searchable_shell(line)
    ]


def _found_elsewhere(
    names: list[str], exclude: str, pattern: str, read: Callable[[str], set[str]]
) -> dict[str, str]:
    """Of NAMES, each one READ finds in some shell file of the merged tree other
    than EXCLUDE, mapped to the first such file.

    Parsed, never matched: this answer decides whether a check stays SILENT, and
    a regex that read an indented mention or a comment as the real thing would
    suppress a true finding with no output at all.

    The WORKING TREE is what `git grep` and the read below see, so a caller the
    resolution itself left untracked is invisible here.
    """
    wanted = {name for name in names if _SEARCHABLE.match(name)}
    if not wanted:
        return {}
    candidates = _shortlist(sorted(wanted), exclude, pattern)
    if len(candidates) > _MAX_RELOCATION_FILES:
        warn(
            f"::warning::undefined-command: {len(candidates)} shell files mention "
            f"one of {', '.join(sorted(wanted))}; read the first "
            f"{_MAX_RELOCATION_FILES} of them."
        )
        candidates = candidates[:_MAX_RELOCATION_FILES]
    found: dict[str, str] = {}
    for path in candidates:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name in wanted & read(text):
            found.setdefault(name, path)
        if len(found) == len(wanted):
            break
    return found


def relocated(names: list[str], exclude: str) -> set[str]:
    """Of NAMES, those another shell file in the merged tree defines.

    A parent that moved a helper into a sourced library dropped it from this
    file legitimately, and the call that survived still resolves. Deciding
    which files this one actually sources needs a shell interpreter, so this
    asks the coarser question: is the name defined anywhere else in the tree.
    """
    return set(_found_elsewhere(names, exclude, _DEFINITION_PATTERN, defined_functions))


def called_elsewhere(names: list[str], exclude: str) -> set[str]:
    """Of NAMES, those another shell file in the merged tree calls.

    A merge that kept the definition here and the only call in a sibling script
    left a helper that still runs, so `orphaned_definitions` reports nothing.
    The same coarse question as `relocated`, asked of calls.
    """
    return set(_found_elsewhere(names, exclude, _CALL_PATTERN, called_names))


def orphaned_definitions(
    base: str, sides: list[str], merged: str, path: str
) -> list[str]:
    """The bash functions one of SIDES added to PATH since BASE and called
    there, that MERGED still defines and the merged tree calls nowhere.

    The shell twin of `_contradictory_merge.orphaned_added_names`, and both
    conditions on the parent are load-bearing for the same reason there: `added`
    alone names a helper written for another script to call, and `called by that
    parent` narrows it to one whose own file used it. A merged file that keeps
    such a definition and calls it nowhere has lost every use of what it kept,
    which is the merge's doing (agent-glovebox#6400 and #6144: the merge took
    one side's inline body and the other side's function, and
    `_kata_channels_stop_records` landed with no caller at all).

    A side no parser could read whole contributes no definitions, which would
    read as an addition, so one unreadable side declines the comparison."""
    if any(_root(text) is None for text in (base, merged, *sides)):
        return []
    defined_at_base = defined_functions(base)
    orphaned = defined_functions(merged) - called_names(merged)
    found: set[str] = set()
    for side in sides:
        added = defined_functions(side) - defined_at_base
        found |= added & orphaned & called_names(side)
    if not found:
        return []
    return sorted(found - called_elsewhere(sorted(found), path))


def shell_seams(sides: list[str], merged: str, path: str) -> list[str]:
    """The names PATH's merged text calls that this resolution left undefined."""
    dropped = undefined_calls(sides, merged)
    if not dropped:
        return []
    moved = relocated(dropped, path)
    return [name for name in dropped if name not in moved]


def sourced_basenames(text: str) -> set[str]:
    """The file basenames TEXT loads with `source` or `.`.

    The grammar finds the command and its first argument, and the basename is
    that argument's last path component: `source "$SCRIPT_DIR/clone.bash"` reads
    as `clone.bash`. A last component an expansion decides is not read at all,
    because a guess there would answer about a file nobody named."""
    root = _root(text)
    if root is None:
        return set()
    out: set[str] = set()
    for node in _reader().walk(root):
        if node.type != "command":
            continue
        name = node.child_by_field_name("name")
        if name is None or not name.children:
            continue
        if _reader().literal(name.children[0]) not in _SOURCE_COMMANDS:
            continue
        args = node.children_by_field_name("argument")
        if not args:
            continue
        tail = args[0].text.decode().rsplit("/", 1)[-1].strip("\"'")
        if _SOURCE_BASENAME.fullmatch(tail):
            out.add(tail)
    return out


def sourced_somewhere(path: str) -> bool:
    """Whether another shell file in the merged tree loads PATH.

    A file nobody sources never puts a function in another file's scope, so a
    call to the same name there was bound to something else all along."""
    basename = path.rsplit("/", 1)[-1]
    if not _SOURCE_BASENAME.fullmatch(basename):
        return False
    return bool(_found_elsewhere([basename], path, _CALL_PATTERN, sourced_basenames))


def dropped_definition_seams(sides: list[str], merged: str, path: str) -> list[str]:
    """The bash functions a parent defined in PATH that the merge dropped, and a
    file sourcing PATH in the merged tree still calls.

    `shell_seams` reads PATH's own call sites, so it names this break only where
    the surviving call sits in the file that lost the definition. A library and
    the script sourcing it are two files, and that caller merges clean, so the
    break reaches neither side of that check (agent-glovebox#6940).

    The ACCUSING file has to source PATH. That is what stops a coarse tree
    search from reading any script that runs `ls`, `log` or `build` as the
    caller of a helper named after that command. The tree-wide question runs
    first only to short-circuit: a file nobody sources exports no function, and
    answering that costs one search where the rest costs two.

    This is weaker than `shell_seams`' same-file proof: a parent that shipped
    the break itself, leaving a stale call in a third file, reads the same way,
    and a caller reaching PATH through a sourced index reads as unrelated.

    An unreadable MERGED file defines and calls nothing, so every function a
    parent bound would read as dropped; the comparison declines instead."""
    if any(_root(text) is None for text in (merged, *sides)):
        return []
    lost = set().union(*(defined_functions(side) for side in sides))
    lost -= defined_functions(merged) | _self_wrapping(sides)
    # Exactly the set `shell_seams` reports, never all of `called_names`: a name
    # a parent defined BELOW its own top-level call is absent from that set, and
    # subtracting the wider one would leave its 127 unnamed by either check.
    lost -= set(undefined_calls(sides, merged))
    if not lost or not sourced_somewhere(path):
        return []
    blind = sorted(name for name in lost if not _SEARCHABLE.match(name))
    if blind:
        warn(
            f"::warning::undefined-command: '{path}' dropped {', '.join(blind)}, "
            "which no tree search can look for; read the merged tree by hand for "
            "a surviving call."
        )
    names = sorted(lost)
    defined_elsewhere = relocated(names, path)
    loaders = [name for name in names if name not in defined_elsewhere]
    basename = path.rsplit("/", 1)[-1]

    def calls_after_loading(text: str) -> set[str]:
        return called_names(text) if basename in sourced_basenames(text) else set()

    return sorted(_found_elsewhere(loaders, path, _CALL_PATTERN, calls_after_loading))


def is_shell_source(path: str, text: str) -> bool:
    """Whether TEXT, the content of PATH at some revision, is a shell script:
    the suffix, or a shell shebang on TEXT's own first line. `is_shell` reads
    the working tree, which is another revision's bytes."""
    if path.endswith(_SHELL_SUFFIXES):
        return True
    first = text.split("\n", 1)[0].encode("utf-8", errors="replace")
    return _SHELL_SHEBANG.match(first) is not None


def is_shell(path: str) -> bool:
    """Whether PATH is a file this check reads.

    The suffix, or a shell shebang. The git hooks carry no suffix at all, and
    a hook that loses a helper this way fails OPEN — the gate it was meant to
    run silently stops running, which is the worst place in a tree to put a
    127 nobody sees."""
    if path.endswith(_SHELL_SUFFIXES):
        return True
    try:
        with open(path, "rb") as handle:
            return _SHELL_SHEBANG.match(handle.readline()) is not None
    except OSError:
        return False
