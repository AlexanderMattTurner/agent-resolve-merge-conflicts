#!/usr/bin/env python3
"""Ban a `permissions.allow` Bash grant whose `*` extends a word.

A grant is matched by `.claude/hooks/lib-checks.sh`'s pattern matcher, which
turns `*` into a wildcard and anchors the result. `Bash(git diff*)` therefore
auto-approves not just `git diff HEAD` but `git difftool`, which executes an
arbitrary command named in git config — the wildcard silently spanned the
token that SELECTS WHAT RUNS.

The rule, structural and hermetic — no table of real command names to drift:

    In a `Bash(<spec>)` entry of `permissions.allow`, a `*` must open the spec
    or follow a DELIMITER — whitespace, a shell metacharacter, `:`, `=`, `/`,
    or a quote.

An allowlist of delimiters, not a denylist of word characters: a command token
may contain `_`, `.`, `-`, `+`, `@`, `~`, `%`, `^` and `,` as readily as a
letter, so naming the word characters leaves `Bash(foo_*)` auto-approving
`foo_bar` and `Bash(pre-*)` auto-approving `pre-commit` — the same defect one
character later. The delimiters below cannot appear inside a command token, so
a `*` after one extends the ARGUMENTS of a command already fully named
(`Bash(git diff *)`, `Bash(pnpm test:*)`) or the CONTENTS of a directory
already fully named (`Bash(./scripts/*)`).

Remedy: the two-form grant — `Bash(git diff)` plus `Bash(git diff *)` — so the
wildcard starts at a delimiter and `git difftool` still prompts.

Scope is `permissions.allow` only; `deny` entries must span everything they
can, the opposite shape. Invoked by pre-commit with the staged settings files.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _linecheck import run_line_checks  # noqa: E402  # pylint: disable=wrong-import-position

_MESSAGE = (
    "a Bash allow-grant's `*` extends a command token, so it spans every longer "
    "command sharing that prefix (`Bash(git diff*)` auto-approves `git difftool`; "
    "`Bash(pre-*)` auto-approves `pre-commit`). Write the two-form grant instead: "
    "`Bash(git diff)` plus `Bash(git diff *)`."
)

# The characters a shell command token cannot contain, so a `*` after one cannot
# extend the token. Everything NOT here is treated as part of a token: an
# allowlist fails closed on a character nobody thought of, where a denylist of
# word characters fails open on it.
#
# A character that CLOSES something is not one of them, because the shell joins
# what it closed to whatever follows. `Bash("git"*)` matches `"git"tool` and
# `Bash($(printf git)*)` matches `$(printf git)tool`; both run `gittool`, the
# prefix approval this check exists to refuse. So no quote, no `)`, and no
# backtick — a backtick opens and closes with the same character, so it fails
# closed.
_DELIMITERS = " \t;|&(<>:=/"

# The whole rule: a `*` immediately preceded by anything that is not a delimiter.
_WORD_EXTENDING = re.compile(rf"[^{re.escape(_DELIMITERS)}]\*")


def spans_a_word(spec: str) -> bool:
    """True when SPEC (the text inside `Bash(...)`) has a `*` immediately
    following a character a command token may contain — the token-extending
    wildcard."""
    return bool(_WORD_EXTENDING.search(spec))


def bash_spec(grant: str) -> str | None:
    """The text inside a `Bash(...)` grant, or None for any other tool's grant."""
    if grant.startswith("Bash(") and grant.endswith(")"):
        return grant[len("Bash(") : -1]
    return None


def _line_of(lines: list[str], needle: str, taken: set[int]) -> int:
    """The 1-based line carrying NEEDLE (a JSON-encoded grant), skipping lines
    already reported so a duplicated grant points at both of its entries."""
    for lineno, line in enumerate(lines, 1):
        if needle in line and lineno not in taken:
            return lineno
    return 1


def violations(text: str) -> list[int]:
    """1-based line numbers of `permissions.allow` Bash grants whose wildcard
    extends a word. A malformed file (not valid JSON) is not this lint's
    concern — the JSON validator hook owns that failure."""
    try:
        allow = json.loads(text).get("permissions", {}).get("allow")
    except json.JSONDecodeError:
        return []
    if not isinstance(allow, list):
        return []
    lines = text.splitlines()
    hits: set[int] = set()
    for grant in allow:
        spec = bash_spec(grant) if isinstance(grant, str) else None
        if spec is None or not spans_a_word(spec):
            continue
        hits.add(_line_of(lines, json.dumps(grant), hits))
    return sorted(hits)


def main(argv: list[str]) -> None:
    sys.exit(run_line_checks(argv, violations, _MESSAGE))


if __name__ == "__main__":
    main(sys.argv[1:])
