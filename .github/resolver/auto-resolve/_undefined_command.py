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

A finding must be provably MERGE-CAUSED: the merged file calls the name, a
parent's version of that same file defines it, and the merged file does not.
That is the standard `_contradictory_merge` holds its own checks to, and it
needs no `PATH` oracle — one would answer about the runner's image rather
than about the repository being merged.
"""

import functools
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Shell paths this check reads per resolution. Parsing is cheap, but the
# relocation search below runs `git grep` once per candidate name, so a
# resolution touching hundreds of scripts would spend the step's budget here.
MAX_PATHS = 60
# Files ONE name's relocation search parses. The pattern below is
# definition-shaped, so a name reaching this many candidates is a name defined
# all over the tree; the truncation is reported rather than silent, because a
# dropped candidate could be the file that suppresses a finding.
_MAX_RELOCATION_FILES = 200
# Shell function names this check can search for. A name outside it is
# reported rather than searched: the `git grep -E` pre-filter below would have
# to escape it into a POSIX ERE, and a mis-escaped pattern silences a finding.
_SEARCHABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SHELL_SUFFIXES = (".sh", ".bash")
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
    module. Crashing there would discard a resolution the model was already
    billed for, so the check stands down and says so in the job log instead.
    """
    try:
        import lib_bash_ast  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        warn(
            f"::warning::undefined-command: no bash parser ({exc}); read none of "
            "the shell in this resolution. A fork head skips the toolchain install "
            "that pins it."
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

    ONE FILE'S two blobs is the deliberate bound. A helper deleted from
    `lib.sh` while another file gains a call to it is the same break, and
    answering it means reading every shell file at BOTH parent shas rather
    than the resolution's own set. `relocated` below already clears the
    common half of that shape: a name the merged tree still defines
    somewhere is never reported."""
    if any(_root(text) is None for text in (merged, *sides)):
        return []
    parents_reach = set().union(*(available_names(side) for side in sides))
    dropped = (parents_reach - available_names(merged)) & called_names(merged)
    return sorted(dropped - _self_wrapping(sides))


def _shortlist(names: list[str], exclude: str) -> list[str]:
    """Shell files other than EXCLUDE whose text defines ANY of NAMES.

    One `git grep` for the whole set, never one per name: a search per name
    makes the check's cost quadratic in a mangled resolution, and the parse
    below reads each file's whole definition set anyway.

    A regex PRE-FILTER, never the answer: it shortlists files for that parse,
    which decides. `git grep` exits 1 on no match, so only a code above that
    is an error."""
    alternation = "|".join(names)
    done = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "-E",
            "-e",
            # BOTH forms bash accepts, because `defined_functions` reads both:
            # `f()`, `f ()`, `function f {`, `function f()`. A parenthesised-only
            # pattern shortlists nothing for a helper relocated as `function f {`,
            # so the finding it fails to suppress costs a correct resolution its
            # auto-merge. The trailing `\(\)|\{` keeps prose out.
            rf"(^|[[:space:]])(function[[:space:]]+)?({alternation})[[:space:]]*(\(\)|\{{)",
            "--",
            *(f"*{suffix}" for suffix in _SHELL_SUFFIXES),
            ".hooks",
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
    return [line for line in done.stdout.splitlines() if line and line != exclude]


def relocated(names: list[str], exclude: str) -> set[str]:
    """Of NAMES, those another shell file in the merged tree defines.

    A parent that moved a helper into a sourced library dropped it from this
    file legitimately, and the call that survived still resolves. Deciding
    which files this one actually sources needs a shell interpreter, so this
    asks the coarser question: is the name defined anywhere else in the tree.

    Parsed, never matched: this answer decides whether to stay SILENT, and a
    regex that read an indented mention or a comment as a definition would
    suppress a real break with no output at all.
    """
    wanted = {name for name in names if _SEARCHABLE.match(name)}
    if not wanted:
        return set()
    candidates = _shortlist(sorted(wanted), exclude)
    if len(candidates) > _MAX_RELOCATION_FILES:
        warn(
            f"::warning::undefined-command: {len(candidates)} shell files look "
            f"like they define one of {', '.join(sorted(wanted))}; read the "
            f"first {_MAX_RELOCATION_FILES} looking for their new home."
        )
        candidates = candidates[:_MAX_RELOCATION_FILES]
    found: set[str] = set()
    for path in candidates:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found |= wanted & defined_functions(text)
        if found == wanted:
            break
    return found


def shell_seams(sides: list[str], merged: str, path: str) -> list[str]:
    """The names PATH's merged text calls that this resolution left undefined."""
    dropped = undefined_calls(sides, merged)
    if not dropped:
        return []
    moved = relocated(dropped, path)
    return [name for name in dropped if name not in moved]


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
