"""A command the CALLING repository named: how it is split, and whether it RAN.

PROBLEM CLASS — two questions about a caller's command, each answered in several
places that disagreed.

The first is how the command line becomes an argv. The shell word-split it in one
step and `shlex` split it in another, so `"my dir/gen.sh" --all` named a different
program to the pre-flight check than to the command that ran. A line whose
quoting does not close is part of that question, and `configured_argv` below is
its whole answer.

The second is whether a non-zero status is a VERDICT about the merged tree or a
report that the command never reached one. Reading the second as the first blames
the branch for the workflow's own provisioning. Only a status no program picks
for itself answers that, so the shared set holds the shell's two
could-not-execute codes and the two spellings of a signal kill. `EXIT_MISCONFIGURED`
is NOT one of them: a pre-commit hook wrapper returns it by convention for "the
tool this gate drives is not provisioned here", so `_hook_gate` names it there,
while a caller's pre-pass or post-merge check is an arbitrary program that can
exit it as its own finding about a configuration file the merge broke.
"""

import argparse
import re
import shlex
import sys

#: The shell's floor for "the command never ran": 126 (found, not executable),
#: 127 (not found) and every 128+signal, which includes an OOM kill. Below it the
#: command RAN and reported, so its status is a verdict.
NEVER_RAN = 126

#: A MISSING DEPENDENCY, named by the interpreter itself, WITH the module's name.
#: Not a traceback in general: a generator that runs and raises over the merged
#: sources prints one too, and reading that as provisioning blames the workflow
#: for the branch.
MISSING_MODULE_RE = re.compile(
    r"""(?:No module named|Cannot find module|ERR_MODULE_NOT_FOUND[^'"]*)"""
    r"""\s*['"](?P<module>[^'"\n]+)['"]"""
)
#: The same class with NO module name to capture — a shell's own wording, and an
#: interpreter's when nothing quotes the name it could not find. A wrapper under
#: `set -e` turns the shell case into exit 127, but a pipeline or a subshell
#: swallows that and exits 1, where this line is the only signal left.
MISSING_TOOL_RE = re.compile(
    r"command not found|Cannot find module|ModuleNotFoundError|ERR_MODULE_NOT_FOUND"
)


def split_argv(command: str) -> list[str]:
    """COMMAND as the argv a shell would build, quoting honoured.

    Raises `ValueError` on a quote that never closes. Every reader here calls
    `configured_argv` instead, which answers that case.
    """
    return shlex.split(command)


def configured_argv(command: str) -> list[str]:
    """COMMAND as an argv, and COMMAND WHOLE when its quoting does not close.

    INVARIANT — this never RAISES, and it never answers EMPTY for a command the
    caller did name. Both failures are silent in the direction that costs a
    resolution: `shlex.split` raises `ValueError`, which ends the step with a
    traceback that names neither the bad input nor the workflow that owns it, and
    every reader takes an empty argv for "this caller declared no command" and
    then skips the pre-pass or the check without a word.

    So the unclosed line survives as ONE word. No runner can execute a filename
    holding an open quote, so `_refusal.run_or_refuse` states the refusal, quotes
    the line back, and leaves the head unmarked for a re-run after the fix.
    """
    try:
        return split_argv(command)
    except ValueError:
        return [command]


def status_never_ran(returncode: int) -> bool:
    """RETURNCODE says the command could not run, rather than what it found.

    INVARIANT — this set holds only statuses NO program picks as a verdict: 126
    and 127, which a shell returns for a command it could not execute; 128+signal,
    which a shell returns for one a signal killed; and the negative code
    `subprocess` returns for that same kill. Admitting a status a program can
    choose would discard that program's real finding as a broken runner.
    """
    return returncode < 0 or returncode >= NEVER_RAN


def missing_tool_line(text: str) -> str | None:
    """The FIRST line of TEXT naming a tool or module that was not there.

    First, not best: a caller quotes this line back to a human as what the run
    saw, and the earliest fault is the one that started every later line.
    """
    for line in text.splitlines():
        if MISSING_TOOL_RE.search(line) or MISSING_MODULE_RE.search(line):
            return line
    return None


def missing_module(text: str) -> str | None:
    """The module name TEXT reports as absent, when an interpreter named one."""
    found = MISSING_MODULE_RE.search(text)
    return found.group("module") if found else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--program", metavar="COMMAND")
    mode.add_argument("--argv", metavar="COMMAND")
    mode.add_argument("--could-not-run", metavar="LOG")
    args = parser.parse_args()

    if args.program is not None:
        # The first word AFTER the split, which is what `command -v` must find on
        # PATH: a quoted program holding whitespace is one word here and two to a
        # split on spaces, so the pre-flight would check a name nothing runs.
        argv = configured_argv(args.program)
        sys.stdout.write(argv[0] if argv else "")
        return
    if args.argv is not None:
        # NUL-terminated, so a quoted argument holding whitespace survives the
        # read that `mapfile -d ''` does on the other side.
        words = configured_argv(args.argv)
        sys.stdout.write("".join(f"{word}\0" for word in words))
        return
    with open(args.could_not_run, encoding="utf-8", errors="replace") as log:
        line = missing_tool_line(log.read())
    if line is None:
        raise SystemExit(1)
    print(line)


if __name__ == "__main__":
    main()
