"""Shared machinery for the line-oriented pre-commit lints under ``checks/``.

The ``checks/*.py`` scripts each scan a list of paths given on argv, read each file as
UTF-8 (skipping anything unreadable), run a per-script detector over the text, and
print ``<path>:<lineno>: <message>`` to stderr for every hit — refusing if any fired.
The read loop, the skip-on-OSError/UnicodeDecodeError, the print loop, and the
refusal live here (``run_line_checks``), so each script body is just its own
detector. A detector that asks about a shell COMMAND rather than a line of text
takes ``_shellcmd.scan_commands``, which drives it over the bash grammar's own view
of the file.

Imported as ``repolint._linecheck``: the scripts run as ``python3
.github/scripts/checks/*.py``, so ``sys.path[0]`` is ``checks/``, one level below the
``repolint`` package; each script prepends ``.github/resolver/`` to ``sys.path`` before
importing it, which also covers the tests, who load each script by path. The package
name is what keeps this module out of ``ci_truth_serum``'s bare-import namespace —
``repolint/__init__.py`` states the collision it prevents.
"""

import sys
from collections.abc import Callable


def strip_comment(line: str) -> str:
    """LINE with a trailing ``#...`` comment cut, honoring single/double quotes.

    PROBLEM CLASS — "cut the shell comment off this line before matching code
    on it". Naive: no escape handling, no heredoc awareness, one physical
    line. A lint that needs more than that reads the bash grammar instead
    (`.github/scripts/checks/_bash_ast.py`).
    """
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def report_line_checks(
    argv: list[str],
    find_violations: Callable[[str], list[int]],
    message: str,
    *,
    remedy: str,
) -> bool:
    """Print every hit a line-oriented lint finds over ARGV; report whether any fired.

    For each readable path, FIND_VIOLATIONS(text) returns the 1-based line numbers
    that violate. Each hit prints ``<path>:<lineno>: <message>`` to stderr; an
    unreadable path (OSError / UnicodeDecodeError) is skipped. After the hits, one
    ``fix: <remedy>`` line prints. REMEDY is a required keyword because the failure
    path is the least-executed code in the repo and fires for a reader without the
    author's context: it must name the action that clears the finding (the edit,
    the helper to route through, or the lint's opt-out annotation) — a detector
    whose author cannot write that sentence is not ready to block commits.

    An empty or whitespace REMEDY is refused with ValueError — it would print a
    bare ``fix:`` line, defeating the requirement it exists to enforce.

    A script running several detectors over the same paths uses this directly so
    every detector reports before the process refuses; a single-detector script
    uses ``run_line_checks``.
    """
    if not remedy.strip():
        raise ValueError("remedy must be a non-empty sentence naming the fix")
    found = False
    for path in argv:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except (  # a tracked path that vanished or is not utf-8 text has no lines to check
            OSError,
            UnicodeDecodeError,
        ):
            continue
        try:
            found_lines = find_violations(text)
        except Exception as err:
            # Name the file the detector choked on: pre-commit hands this loop the
            # whole staged list, so the parse refusal alone ("fix the construct the
            # grammar chokes on") names no file to fix. `from err` keeps the original
            # type, message and traceback in the chain.
            raise RuntimeError(f"while scanning {path}") from err
        for lineno in found_lines:
            print(f"{path}:{lineno}: {message}", file=sys.stderr)
            found = True
    if found:
        print(f"fix: {remedy}", file=sys.stderr)
    return found


def run_line_checks(
    argv: list[str],
    find_violations: Callable[[str], list[int]],
    message: str,
    *,
    remedy: str,
) -> None:
    """Drive a single-detector line-oriented lint over ARGV, refusing on any hit."""
    if report_line_checks(argv, find_violations, message, remedy=remedy):
        raise SystemExit(1)
