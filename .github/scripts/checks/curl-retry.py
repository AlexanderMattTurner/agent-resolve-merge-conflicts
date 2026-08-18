#!/usr/bin/env python3
"""Require a retry on file-writing ``curl`` downloads in this repo's shell.

A single-shot ``curl … -o <file>`` has no resilience to a transient network
blip: on a flaky link or a rate-limited shared-cloud IP it fails the whole
install for one dropped packet. This flags an invocation that runs ``curl``
and writes to a file (``-o``/``--output``) without a ``--retry`` flag and not
wrapped in this repo's retry helper (``retry``/``retry_stdout``,
``.github/scripts/lib-ci-retry.sh``).

Destinations that cannot hold a partial download are out of scope: ``-``
(stdout, captured into a variable) and ``/dev/null`` (a discard, so the
transfer is a measurement, not a download).

A site that must stay single-shot opts out with a
``# curl-retry-ok: <reason>`` on the command's line or the line above.

Simplified from the source check this was ported from: it reads only the
command node the parser sees, so a `curl` invoked through an unrecognized
wrapper or a name built from a variable is not caught, and a `--retry` on a
different curl invocation in the same script does not count for this one.
"""

import re
import sys
from pathlib import Path

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _linecheck import run_line_checks  # noqa: E402  # pylint: disable=wrong-import-position

_ALLOW_RE = re.compile(r"#\s*curl-retry-ok:\s*\S")

_NO_FILE_DESTINATIONS = frozenset({"-", "/dev/null"})
_RETRY_WRAPPERS = frozenset({"retry", "retry_stdout"})

_PARSER = Parser(Language(tree_sitter_bash.language()))


def _literal(node: Node) -> str | None:
    if node.type in ("word", "number"):
        return node.text.decode()
    if node.type == "raw_string":
        return node.text.decode()[1:-1]
    if node.type == "string" and all(
        c.type == "string_content" for c in node.children[1:-1]
    ):
        return node.text.decode()[1:-1]
    return None


def _command_words(node: Node) -> list[str | None] | None:
    if node.type != "command":
        return None
    name_node = node.child_by_field_name("name")
    if name_node is None or not name_node.children:
        return None
    name = _literal(name_node.children[0])
    if name is None:
        return None
    args = [_literal(c) for c in node.children_by_field_name("argument")]
    return [name, *args]


def _output_flag(word: str) -> bool:
    """True when WORD is curl's ``-o``/``--output`` flag, including a bundled
    short-flag tail (`-fsSLo` == `-f -s -S -L -o`); the `o` must be the flag
    cluster's LAST letter, so `--connect-timeout` is not mistaken for it."""
    if word == "--output":
        return True
    return (
        word.startswith("-")
        and not word.startswith("--")
        and word[1:].isalpha()
        and word.endswith("o")
    )


def _writes_a_file(words: list[str | None]) -> bool:
    for i, word in enumerate(words):
        if word is None:
            continue
        if word.startswith("--output="):
            if word.removeprefix("--output=") not in _NO_FILE_DESTINATIONS:
                return True
        elif _output_flag(word):
            destination = words[i + 1] if i + 1 < len(words) else ""
            if destination not in _NO_FILE_DESTINATIONS:
                return True
    return False


def _unretried_download(words: list[str | None]) -> bool:
    if "curl" not in words:
        return False
    if not _writes_a_file(words):
        return False
    string_words = {w for w in words if w is not None}
    if _RETRY_WRAPPERS & string_words:
        return False
    return not any(w and w.startswith("--retry") for w in words)


def _walk(node: Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _suppressed_lines(root: Node) -> set[int]:
    lines: set[int] = set()
    for node in _walk(root):
        if node.type != "comment":
            continue
        if _ALLOW_RE.search(node.text.decode()):
            lines.add(node.start_point[0] + 1)
    return lines


def violations(text: str) -> list[int]:
    """1-based line numbers running a file-writing ``curl`` with no retry."""
    root = _PARSER.parse(text.encode()).root_node
    exempt = _suppressed_lines(root)
    hits: list[int] = []
    for node in _walk(root):
        words = _command_words(node)
        if not words or not _unretried_download(words):
            continue
        line = node.start_point[0] + 1
        if line in exempt or (line - 1) in exempt:
            continue
        hits.append(line)
    return sorted(hits)


def main(argv: list[str]) -> None:
    sys.exit(
        run_line_checks(
            argv,
            violations,
            "single-shot `curl … -o` download with no retry — a transient blip "
            "fails the install. Add `--retry 3 --retry-delay 2` (or wrap in "
            "`retry`/`retry_stdout` from lib-ci-retry.sh), or annotate "
            "`# curl-retry-ok: <reason>`.",
        )
    )


if __name__ == "__main__":
    main(sys.argv[1:])
