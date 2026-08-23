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
_DELIMITERS = " \t;|&(<>=/"

# `:` separates a command from its argument in the shapes a grant blesses
# (`Bash(pnpm test:*)`), but Bash runs a file whose NAME contains a colon, so in
# the executable word itself `Bash(foo:*)` matches `foo:tool` and approves a
# different program. A delimiter after the command, never inside it.
_AFTER_THE_COMMAND = ":"


# What starts a new command inside one grant, so the word after it is another
# executable: `Bash(echo ok;foo:*)` ends in the executable `foo:`.
_SEPARATORS = ";|&(\n"


def _extends_a_token(spec: str, star: int) -> bool:
    """Does the `*` at index STAR continue the word before it?

    A `*` opening the spec extends nothing. Otherwise the character before it
    decides, and the EXECUTABLE word is stricter than the rest: nothing
    separates a command from a longer command sharing its name. Which word is
    the executable is read from the last separator, not from the start of the
    spec.
    """
    if star == 0:
        return False
    before = spec[:star]
    since = before[max((before.rfind(c) for c in _SEPARATORS), default=-1) + 1 :]
    in_command = not any(space in since.lstrip() for space in " \t")
    delimiters = _DELIMITERS if in_command else _DELIMITERS + _AFTER_THE_COMMAND
    return spec[star - 1] not in delimiters


def spans_a_word(spec: str) -> bool:
    """True when SPEC (the text inside `Bash(...)`) has a `*` immediately
    following a character a command token may contain — the token-extending
    wildcard."""
    return any(_extends_a_token(spec, i) for i, c in enumerate(spec) if c == "*")


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
