"""Shared tree-sitter reading of JavaScript and TypeScript for the merge checks.

PROBLEM CLASS — read a JS/TS module's top-level DEFINITIONS and EXPORTS from its
grammar rather than its text. Both `_contradictory_merge` (a definition kept
twice) and `dropped_name_seams` (an export a caller still uses) ask it, and a
copy per check would drift on which declaration forms bind a name.

The grammar is chosen by the path's SUFFIX, never by sniffing content: a `.ts`
file read as JavaScript is a tree of ERROR nodes from its first type annotation.
A file that does not parse cleanly answers None, never a guess, because each
caller compares two versions and a half-parsed side misattributes what it finds.

The parser is imported lazily. `install-hook-tools.sh` pins and asserts it, but
that step is skipped on a fork head, where `bundle.py` still imports both
checks; there each reader warns once and reads no JS/TS.
"""

import functools
import sys
from collections import Counter
from pathlib import Path

_GRAMMARS = {
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
}
# Statements that bind their `name` field once.
_NAMED_DECLARATIONS = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "class_declaration",
        "abstract_class_declaration",
    }
)
_VARIABLE_DECLARATIONS = frozenset({"lexical_declaration", "variable_declaration"})
# Exported, but not counted as a duplicate: TypeScript merges a second
# interface or enum of one name into the first by design.
_TYPE_DECLARATIONS = frozenset(
    {"interface_declaration", "type_alias_declaration", "enum_declaration"}
)


def is_js(path: str) -> bool:
    """Whether PATH names a JavaScript or TypeScript file this module reads."""
    return Path(path).suffix in _GRAMMARS


@functools.cache
def _parsers() -> dict | None:
    """Grammar name -> tree-sitter Parser, or None when no grammar is installed."""
    try:
        # pylint: disable=import-outside-toplevel
        import tree_sitter_javascript
        import tree_sitter_typescript
        from tree_sitter import Language, Parser
    except ImportError as exc:
        print(
            f"::warning::js-parser: a JS/TS grammar is not installed ({exc}), so no "
            "JavaScript or TypeScript file is read in this job.",
            file=sys.stderr,
            flush=True,
        )
        return None
    return {
        name: Parser(Language(language))
        for name, language in (
            ("javascript", tree_sitter_javascript.language()),
            ("typescript", tree_sitter_typescript.language_typescript()),
            ("tsx", tree_sitter_typescript.language_tsx()),
        )
    }


def parse_clean(text: str, path: str):
    """TEXT's root node under PATH's grammar, or None when PATH is not JS/TS, no
    grammar is installed, or the tree holds an ERROR or MISSING node."""
    grammar = _GRAMMARS.get(Path(path).suffix)
    parsers = _parsers() if grammar else None
    if parsers is None:
        return None
    root = parsers[grammar].parse(text.encode()).root_node
    return None if root.has_error else root


def _pattern_names(node) -> list[str]:
    """The names a declarator's NAME pattern binds. A default value or an object
    key is read, never bound, so neither is walked."""
    if node.type in ("identifier", "shorthand_property_identifier_pattern"):
        return [node.text.decode()]
    if node.type == "pair_pattern":
        return _pattern_names(node.child_by_field_name("value"))
    if node.type in ("assignment_pattern", "object_assignment_pattern"):
        return _pattern_names(node.child_by_field_name("left"))
    if node.type in ("object_pattern", "array_pattern", "rest_pattern"):
        return [name for child in node.named_children for name in _pattern_names(child)]
    return []


def _named(node) -> list[str]:
    """NODE's `name` field, or none for an anonymous `export default class {}`."""
    name = node.child_by_field_name("name")
    return [] if name is None else [name.text.decode()]


def _declared(node) -> list[str]:
    """The names a function, class or `const`/`let`/`var` statement binds."""
    if node.type in _NAMED_DECLARATIONS:
        return _named(node)
    if node.type in _VARIABLE_DECLARATIONS:
        return [
            name
            for declarator in node.named_children
            if declarator.type == "variable_declarator"
            for name in _pattern_names(declarator.child_by_field_name("name"))
        ]
    return []


def _imported(node) -> list[str]:
    """The local names an `import` statement binds. `import "x"` binds none."""
    names: list[str] = []
    for clause in node.named_children:
        if clause.type != "import_clause":
            continue
        for part in clause.named_children:
            if part.type == "identifier":
                names.append(part.text.decode())
            elif part.type == "namespace_import":
                names += [c.text.decode() for c in part.named_children]
            elif part.type == "named_imports":
                for spec in part.named_children:
                    bound = spec.child_by_field_name(
                        "alias"
                    ) or spec.child_by_field_name("name")
                    if spec.type == "import_specifier" and bound is not None:
                        names.append(bound.text.decode())
    return names


def top_level_definitions(text: str, path: str) -> Counter[str] | None:
    """NAME -> how many top-level statements of TEXT bind it: an `import`, a
    function or class declaration, a `const`/`let`/`var` declarator, or the
    `export` form of one. None when TEXT does not parse under PATH's grammar.
    An overload signature binds nothing, so only its implementation counts."""
    root = parse_clean(text, path)
    if root is None:
        return None
    found: Counter[str] = Counter()
    for node in root.named_children:
        if node.type == "import_statement":
            found.update(_imported(node))
        elif node.type == "export_statement":
            declaration = node.child_by_field_name("declaration")
            found.update(_declared(declaration) if declaration else [])
        else:
            found.update(_declared(node))
    return found


def exported_names(text: str, path: str) -> set[str] | None:
    """Every name TEXT exports by name: `export function/class/const`, an
    exported interface, type or enum, `export { a, b as c }`, and
    `export * as ns`. A default export is imported under any name, so it is
    not one. None when TEXT does not parse under PATH's grammar."""
    root = parse_clean(text, path)
    if root is None:
        return None
    names: set[str] = set()
    for node in root.named_children:
        if node.type != "export_statement" or any(
            child.type == "default" for child in node.children
        ):
            continue
        declaration = node.child_by_field_name("declaration")
        if declaration is not None:
            names.update(_declared(declaration))
            if declaration.type in _TYPE_DECLARATIONS:
                names.update(_named(declaration))
        for part in node.named_children:
            if part.type == "namespace_export":
                names.update(
                    c.text.decode() for c in part.named_children if c.type != "string"
                )
            if part.type != "export_clause":
                continue
            for spec in part.named_children:
                exported = spec.child_by_field_name(
                    "alias"
                ) or spec.child_by_field_name("name")
                if exported is not None and exported.type != "string":
                    names.add(exported.text.decode())
    names.discard("default")
    return names
