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

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Shell paths this check reads per resolution. Parsing is cheap, but the
# relocation search below runs `git grep` once per candidate name, so a
# resolution touching hundreds of scripts would spend the step's budget here.
MAX_PATHS = 60
# Files a name's relocation search parses. A common name matches widely, and a
# name defined in one of the first few is already suppressed.
_MAX_RELOCATION_FILES = 20
# Shell function names this check can search for. A name outside it is
# reported rather than searched: the `git grep -E` pre-filter below would have
# to escape it into a POSIX ERE, and a mis-escaped pattern silences a finding.
_SEARCHABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SHELL_SUFFIXES = (".sh", ".bash")


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


def defined_functions(text: str) -> set[str]:
    """Every function TEXT defines, read from the bash grammar."""
    reader = _reader()
    if reader is None:
        return set()
    names = set()
    for node in reader.walk(reader.parse(text)):
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
    reader = _reader()
    if reader is None:
        return set()
    names = set()
    for node in reader.walk(reader.parse(text)):
        words = reader.command_words(node)
        if words:
            names.add(words[0])
    return names


def undefined_calls(sides: list[str], merged: str) -> list[str]:
    """Names MERGED calls that a parent's version of the same file defined.

    SIDES are that file's two parent blobs. A name both parents had already
    dropped is not here: the merge did not drop it, so the call it left is a
    break a parent shipped rather than one this resolution made."""
    merged_defines = defined_functions(merged)
    parents_defined = set().union(*(defined_functions(side) for side in sides))
    return sorted((parents_defined - merged_defines) & called_names(merged))


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
            rf"(^|[[:space:]])({name}|function[[:space:]]+{name})[[:space:]]*\(",
            "--",
            *(f"*{suffix}" for suffix in _SHELL_SUFFIXES),
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
        for path in _shortlist(name, exclude)[:_MAX_RELOCATION_FILES]:
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
    return [name for name in dropped if name not in relocated(dropped, path)]


def is_shell(path: str) -> bool:
    """Whether PATH is a file this check reads.

    Suffix only. A `#!/bin/bash` script with no suffix exists, and reading the
    shebang would mean opening every extensionless path in the resolution to
    find the handful this check could then judge."""
    return path.endswith(_SHELL_SUFFIXES)
