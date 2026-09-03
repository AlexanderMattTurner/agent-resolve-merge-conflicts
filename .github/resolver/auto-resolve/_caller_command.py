"""A command the CALLING repository named: how it is split, and whether it RAN.

PROBLEM CLASS — two questions about a caller's command, each answered in several
places that disagreed.

The first is how the command line becomes an argv. The shell word-split it in one
step and `shlex` split it in another, so `"my dir/gen.sh" --all` named a different
program to the pre-flight check than to the command that ran.

The second is whether a non-zero status is a VERDICT about the merged tree or a
report that the command never reached one. Reading the second as the first blames
the branch for the workflow's own provisioning. Exit 78 was the disagreement: the
hook gate read it as "this job is under-provisioned", the tool verdict read it as
a judgement about the tree. It means under-provisioned, here and everywhere —
BSD's EX_CONFIG, which is also this tree's own EXIT_MISCONFIGURED.
"""

import argparse
import re
import shlex
import sys

#: EX_CONFIG. A caller's wrapper returns it for "the tool this gate drives is not
#: provisioned here", and prepare.sh exits it for its own misconfiguration.
EX_CONFIG = 78
#: The shell's floor for "the command never ran": 126 (found, not executable),
#: 127 (not found) and every 128+signal, which includes an OOM kill. Below it the
#: command RAN and reported, so its status is a verdict.
NEVER_RAN = 126

#: A MISSING DEPENDENCY, named by the interpreter itself. Not a traceback in
#: general: a generator that runs and raises over the merged sources prints one
#: too, and reading that as provisioning blames the workflow for the branch.
MISSING_MODULE_RE = re.compile(
    r"""(?:No module named|Cannot find module|ERR_MODULE_NOT_FOUND[^'"]*)"""
    r"""\s*['"](?P<module>[^'"\n]+)['"]"""
)
#: The same class named by a SHELL rather than by an interpreter. A wrapper under
#: `set -e` turns it into exit 127, but a pipeline or a subshell swallows that and
#: exits 1, where this line is the only signal left.
MISSING_TOOL_RE = re.compile(
    r"^.*(?:command not found|ModuleNotFoundError|ERR_MODULE_NOT_FOUND).*$",
    re.MULTILINE,
)


def split_argv(command: str) -> list[str]:
    """COMMAND as the argv a shell would build, quoting honoured."""
    return shlex.split(command)


def program_of(command: str) -> str:
    """COMMAND's program name — what `command -v` must find on PATH."""
    argv = split_argv(command)
    return argv[0] if argv else ""


def status_never_ran(returncode: int) -> bool:
    """RETURNCODE says the command could not run, rather than what it found."""
    return returncode < 0 or returncode == EX_CONFIG or returncode >= NEVER_RAN


def missing_tool_line(text: str) -> str | None:
    """The first line of TEXT naming a tool or module that was not there."""
    named = MISSING_MODULE_RE.search(text)
    if named:
        return named.group(0)
    line = MISSING_TOOL_RE.search(text)
    return line.group(0) if line else None


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
        sys.stdout.write(program_of(args.program))
        return
    if args.argv is not None:
        # NUL-terminated, so a quoted argument holding whitespace survives the
        # read that `mapfile -d ''` does on the other side.
        sys.stdout.write("".join(f"{word}\0" for word in split_argv(args.argv)))
        return
    with open(args.could_not_run, encoding="utf-8", errors="replace") as log:
        line = missing_tool_line(log.read())
    if line is None:
        raise SystemExit(1)
    print(line)


if __name__ == "__main__":
    main()
