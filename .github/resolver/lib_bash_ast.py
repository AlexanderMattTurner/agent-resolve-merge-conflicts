"""Shared tree-sitter-bash reading for every shell reader in this tree.

PROBLEM CLASS — read a shell script's GRAMMAR rather than its text. Each
reader asks the same three questions: the static text of a word, the
``[name, *args]`` of a simple command, and the lines an ``# <marker>
<reason>`` annotation exempts. A copy per reader drifts on the quoting forms
it resolves and on how wide the annotation window is, so each one answers
"is this suppressed?" differently.

Both sides of the tree read it, as ``lib_credential_ladder`` is read: the
repository's own lints under ``.github/scripts/checks/`` and the resolver's
merge checks under ``auto-resolve/``. It sits at the resolver root because
that subtree is what a consumer clones, and a resolver check may import
nothing outside it. Every caller prepends this directory to ``sys.path``.
"""

import re
from collections.abc import Iterator

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

_PARSER = Parser(Language(tree_sitter_bash.language()))

# Prefixes that only decorate the command that follows them, so the name a
# reader wants is the next word: `command grep -q x` runs grep, not `command`.
_WRAPPERS = frozenset({"command", "builtin", "exec"})


def parse(text: str | bytes) -> Node:
    """The root node of TEXT read as bash.

    Bytes as well as str: a lint reads a script off disk without deciding it is
    UTF-8, and a shell script is bytes to bash."""
    return _PARSER.parse(text.encode() if isinstance(text, str) else text).root_node


def parse_clean(text: str | bytes) -> Node | None:
    """The root node of TEXT, or None when the grammar could not read it all.

    tree-sitter recovers from a syntax error by wrapping the region in an
    ERROR node and parsing on, so a caller that does not ask sees a tree with
    a hole in it: the definitions inside the hole are gone while the calls
    around it survive. A reader whose answer is a COMPARISON between two such
    trees must decline instead — a half-parsed side misattributes what it
    finds.
    """
    root = parse(text)
    return None if root.has_error else root


def walk(node: Node) -> Iterator[Node]:
    """NODE and every node below it."""
    yield node
    for child in node.children:
        yield from walk(child)


def literal(node: Node) -> str | None:
    """The static text of a word, or None when an expansion decides it at run time."""
    if node.type in ("word", "number"):
        return node.text.decode()
    if node.type == "raw_string":
        return node.text.decode()[1:-1]
    if node.type == "string" and all(
        c.type == "string_content" for c in node.children[1:-1]
    ):
        return node.text.decode()[1:-1]
    return None


def command_words(node: Node) -> list[str | None] | None:
    """``[name, *args]`` for a ``command`` node, or None when the stage is not
    a plain command or its name is built from an expansion. An argument this
    module cannot read literally is None, so a caller sees its position."""
    if node.type != "command":
        return None
    name_node = node.child_by_field_name("name")
    if name_node is None or not name_node.children:
        return None
    name = literal(name_node.children[0])
    if name is None:
        return None
    args = [literal(c) for c in node.children_by_field_name("argument")]
    # A bare wrapper runs its argument as the command. One carrying its own
    # flags (`command -v head`) does not, so it is left alone.
    while name in _WRAPPERS and args and args[0] and not args[0].startswith("-"):
        name, args = args[0], args[1:]
    return [name, *args]


def suppressed_lines(root: Node, marker: str) -> set[int]:
    """The 1-based lines a ``# MARKER <reason>`` comment exempts under ROOT:
    the comment's own line, and the first line below it that is not itself a
    comment. A comment with no reason after MARKER exempts nothing.

    PROBLEM CLASS — a one-line window silently stops covering its site the
    moment a SECOND annotation is written above it, because the site now sits
    two lines down. A site needing two markers is normal (a download is both
    unretried and unpinned), and the failure reads as a fresh violation of the
    check whose annotation lost the race. Walking past the comment block is
    what makes annotation ORDER stop mattering, and it matches the window
    ci-truth-serum's ``_linecheck.annotation_window`` already uses.
    """
    allow = re.compile(rf"#\s*{re.escape(marker)}\s*\S")
    source = root.text.decode().splitlines()
    # Only a WHOLE-line comment is walked past. A comment trailing real code
    # sits on the site's own line, so skipping it would step over the very line
    # the annotation above it is there to cover.
    whole_line_comments = {
        node.start_point[0] + 1
        for node in walk(root)
        if node.type == "comment"
        and source[node.start_point[0]].lstrip().startswith("#")
    }
    lines: set[int] = set()
    for node in walk(root):
        if node.type != "comment":
            continue
        if not allow.search(node.text.decode()):
            continue
        line = node.start_point[0] + 1
        lines.add(line)
        below = line + 1
        while below in whole_line_comments:
            below += 1
        lines.add(below)
    return lines
