"""Run one generator as `python GENERATOR` would, but end it before it reads
Python the resolution wrote.

Usage: python3 _generator_guard.py GENERATOR WRITTEN...

INVARIANT: a region counts as re-derived only when the code that rewrote it is
code a parent carried. WRITTEN names the Python files the resolution wrote. The
audit hook below ends the process with exit 3 when the generator opens one of
them (an import, an `exec` of its text, a read), and when it starts any child
process, which this hook cannot see into. Python raises the `open` event
before the file is read, so no written code runs before the refusal.
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
    }
)


def main() -> None:
    """Install the hook, then run GENERATOR as the `__main__` script."""
    generator, *written = sys.argv[1:]
    tainted = {os.path.realpath(path) for path in written}

    def refuse(reason: str) -> None:
        sys.stderr.write(f"{generator} {reason}, so its output proves nothing\n")
        sys.stderr.flush()
        os._exit(3)

    def hook(event: str, args: tuple) -> None:
        if event in _SPAWNS:
            refuse(f"started a process ({event})")
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
