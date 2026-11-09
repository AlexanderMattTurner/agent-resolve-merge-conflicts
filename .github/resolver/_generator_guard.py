"""Run one generator as `python GENERATOR` would, but end it before it opens
a Python file the resolution wrote.

Usage: python3 _generator_guard.py GENERATOR, with the written paths on stdin,
each ending in NUL. Stdin, not argv, so a large merge cannot hit E2BIG.

INVARIANT: a region counts as re-derived only when the code that rewrote it is
code a parent carried. The written paths name the `.py`/`.pyc` files the
resolution wrote. The audit hook below ends the process with exit 3 when:
- the generator opens one of them (an import, a read). Python raises the
  `open` event before the file is read, so no written code runs first;
- it starts a child process, whose imports this hook cannot see;
- it imports `multiprocessing` or a subinterpreter module. A spawn or
  forkserver child starts through `_posixsubprocess` with no audit event, and
  a subinterpreter runs without this interpreter's hooks.
A generator built to dodge the hook (`os.open` with `dir_fd`, `exec` of text
read from a non-`.py` name) is out of scope: the generator itself is parent code.
"""

import os
import runpy
import sys

_SPAWNS = frozenset(
    {
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.spawn",
        "os.system",
        "pty.spawn",
        "subprocess.Popen",
        "cpython.PyInterpreterState_New",
    }
)
_UNHOOKED_RUNTIMES = frozenset(
    {"multiprocessing", "_interpreters", "_xxsubinterpreters"}
)


def main() -> None:
    """Install the hook, then run GENERATOR as the `__main__` script."""
    (generator,) = sys.argv[1:]
    tainted = {os.path.realpath(path) for path in sys.stdin.read().split("\0") if path}

    def refuse(reason: str) -> None:
        sys.stderr.write(f"{generator} {reason}, so its output proves nothing\n")
        sys.stderr.flush()
        os._exit(3)

    def hook(event: str, args: tuple) -> None:
        if event in _SPAWNS:
            refuse(f"started a child runtime ({event})")
        if event == "import" and args[0].partition(".")[0] in _UNHOOKED_RUNTIMES:
            refuse(f"imported {args[0]}, whose children run without this hook")
        if event != "open" or isinstance(args[0], int):
            return
        path = os.fsdecode(args[0])
        if os.path.realpath(path) in tainted:
            refuse(f"opened {path}, which the resolution wrote")

    sys.addaudithook(hook)
    sys.argv = [generator]
    sys.path[0] = os.path.dirname(os.path.abspath(generator))
    runpy.run_path(generator, run_name="__main__")


if __name__ == "__main__":
    main()
