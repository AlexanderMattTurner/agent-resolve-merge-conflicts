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
# Names whose definition a parent may delete on purpose, because deleting a
# WRAPPER around a real command restores the command. `grep() { command grep
# --color=never "$@"; }` is the shape: the merge drops the function and every
# call still resolves, so reporting one would cost auto-merge on a correct
# resolution. Suppression only, so a name missing here is reported, not hidden.
_WRAPPABLE = frozenset(
    """awk basename cat cd chmod chown cp curl cut date diff dirname echo env
    find grep head jq kill ln ls mkdir mv printf ps pwd read rm rmdir sed seq
    sh sleep sort tail tar tee test touch tr uname uniq wc wget xargs""".split()
)


def warn(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@functools.lru_cache(maxsize=1)
def _reader():
    """`lib_bash_ast`, or None when tree-sitter is not installed.

    Imported here rather than at module scope. `bundle.py` imports this module
    on every resolution, and the resolver installs the hook toolchain the
    TARGET repository declares — a repository with no bash hooks has no parser.
    Crashing there would discard a resolution the model was already billed for,
    so the check stands down and says so in the job log instead.
    """
    try:
        import lib_bash_ast  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        warn(
            f"::warning::undefined-command: no bash parser ({exc}); read none of "
            "the shell in this resolution. Install tree-sitter-bash to enable it."
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


def called_names(text: str) -> set[str]:
    """Every name TEXT invokes as a command.

    `command_words` answers None for a name an expansion decides at run time
    (`$helper "$f"`), which this check cannot judge — and does not clear."""
    root = _root(text)
    if root is None:
        return set()
    names = set()
    for node in _reader().walk(root):
        words = _reader().command_words(node)
        if words:
            names.add(words[0])
    return names


def undefined_calls(sides: list[str], merged: str) -> list[str]:
    """Names MERGED calls that a parent's version of the same file defined.

    SIDES are that file's two parent blobs. A name both parents had already
    dropped is not here: the merge did not drop it, so the call it left is a
    break a parent shipped rather than one this resolution made.

    A side no parser could read whole contributes no definitions, which would
    read as a drop, so one unreadable side declines the whole comparison."""
    if any(_root(text) is None for text in (merged, *sides)):
        return []
    merged_defines = defined_functions(merged)
    parents_defined = set().union(*(defined_functions(side) for side in sides))
    dropped = (parents_defined - merged_defines) & called_names(merged)
    return sorted(dropped - _WRAPPABLE)


def _shortlist(name: str, exclude: str) -> list[str]:
    """Shell files other than EXCLUDE whose text mentions NAME as a definition.

    A regex PRE-FILTER, never the answer: it shortlists files for the parse
    below, which decides. `git grep` exits 1 on no match, so only a code above
    that is an error."""
    done = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "-E",
            "-e",
            # BOTH definition forms bash accepts, because `defined_functions`
            # below reads both: `f()`, `f ()`, `function f {` and
            # `function f()`. A pattern matching only the parenthesised form
            # shortlists nothing for a helper relocated as `function f {`, and
            # the finding it fails to suppress costs a correct resolution its
            # auto-merge. The trailing `\(\)|\{` is what keeps prose out.
            rf"(^|[[:space:]])(function[[:space:]]+)?{name}[[:space:]]*(\(\)|\{{)",
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
            f"looking for '{name}': {done.stderr.strip()}"
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
    found = set()
    for name in names:
        if not _SEARCHABLE.match(name):
            continue
        candidates = _shortlist(name, exclude)
        if len(candidates) > _MAX_RELOCATION_FILES:
            warn(
                f"::warning::undefined-command: '{name}' is defined in "
                f"{len(candidates)} files; read the first "
                f"{_MAX_RELOCATION_FILES} looking for its new home."
            )
            candidates = candidates[:_MAX_RELOCATION_FILES]
        for path in candidates:
            try:
                text = Path(path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if name in defined_functions(text):
                found.add(name)
                break
    return found


def shell_seams(sides: list[str], merged: str, path: str) -> list[str]:
    """The names PATH's merged text calls that this resolution left undefined."""
    dropped = undefined_calls(sides, merged)
    if not dropped:
        return []
    # Hoisted, not called per name: `relocated` spends one `git grep` and up to
    # `_MAX_RELOCATION_FILES` parses for EACH name it is handed.
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
