#!/usr/bin/env python3
"""Ban an unvalidated environment variable inside bash `$(( ))` arithmetic.

An env var read directly inside `$(( ))` (`$((SECONDS + ${TIMEOUT:-90}))`)
trusts its value to be an integer. It routinely is not: a typo or an empty
export makes the expansion an arithmetic SYNTAX ERROR that aborts a `set -e`
caller mid-run, and some garbage values coerce to 0, silently disabling the
limit the arithmetic implements.

Remedy: bind the value through a validated variable FIRST
(`[[ "$v" =~ ^[0-9]+$ ]] || v=<default>`), then use that variable in the
arithmetic.

Scope: this repo's ALL-CAPS convention for an externally-set variable — a
lowercase local (a loop counter, an already-validated variable) is not
flagged, since only a name that reads as environment-sourced carries the
"might not be an integer" risk this lint exists for.

Per-line opt-out: a trailing `# env-arith-ok: <reason>` (the reason is
required).

Simplified from the source check this was ported from: that version banned one
project-specific env-var prefix with an explicit grandfather list of prior
offenders; this one generalizes to any ALL-CAPS token (bash's own convention
for an exported/environment name) and carries no grandfather list, since the
target tree has none of its own yet. Known blind spot: the scan is per
physical line, so a `$(( ))` expression spanning several lines is not seen.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _linecheck import run_line_checks  # noqa: E402  # pylint: disable=wrong-import-position

# `$(( ... ))`, one physical line, allowing one level of nested parens.
_ARITH_RE = re.compile(r"\$\(\((?:[^()]|\([^()]*\))*\)\)")
# An ALL-CAPS token of at least two characters: bare (arithmetic context reads
# a name directly), `$NAME`, or `${NAME}`.
_VAR_RE = re.compile(r"\$?\{?\b([A-Z][A-Z0-9_]{1,})\b\}?")
_MARKER_RE = re.compile(r"#\s*env-arith-ok:\s*\S")

# Bash's own builtins, always an integer by construction — never a caller's env.
_BUILTINS = frozenset({"SECONDS", "RANDOM", "LINENO", "BASHPID", "PPID", "UID", "EUID"})


def _strip_comment(line: str) -> str:
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


def violations(text: str) -> list[int]:
    """1-based line numbers where an ALL-CAPS var sits inside `$(( ))`."""
    hits: list[int] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if _MARKER_RE.search(raw):
            continue
        code = _strip_comment(raw)
        for span in _ARITH_RE.finditer(code):
            names = {m for m in _VAR_RE.findall(span.group()) if m not in _BUILTINS}
            if names:
                hits.append(lineno)
                break
    return hits


def main(argv: list[str]) -> None:
    sys.exit(
        run_line_checks(
            argv,
            violations,
            "an ALL-CAPS (env-sourced) variable inside $(( )) — a non-integer "
            "value is an arithmetic syntax error that aborts a set -e caller, "
            "and garbage coerced to 0 silently disables the limit. Validate it "
            "into a variable first, or annotate `# env-arith-ok: <reason>`.",
        )
    )


if __name__ == "__main__":
    main(sys.argv[1:])
