#!/usr/bin/env python3
"""Auto-resolve — DROPPED-NAME SEAMS check.

PROBLEM CLASS — a merge dropped a public name whose callers merged cleanly
and still reference it, so no conflict pointed at the break (agent-glovebox
#4492: metrics.py lost --ref/MAIN_REF/on_main; the caller merged clean and
exited 2 on every run, calling a flag nothing in the conflict named).

Compares each declined path's base and merge blobs with `ast` — Python only,
no regex fallback: a language with no parser here (JS/shell/YAML) is out of
scope, never a guess. Extracts module-level identifiers and argparse
`--flag` strings, drops short/common names, and greps the merged tree for a
surviving caller.

Noise filters (the check's whole contract — widen these before the run
drowns in false positives): identifiers under 4 chars, leading `_`, or in
{main, args, argv, parser, logger, data, name, path, test, debug, true,
false, none} are dropped; flags shorter than `--` + 3 chars or in {--help,
--verbose, --version, --debug, --quiet, --force, --output, --input,
--config, --file, --dry-run} are dropped. Caps: 20 names per file per
category, 40 names per run, 5 referencing paths and 3 lines per name.
"""

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

_PER_FILE_CATEGORY_CAP = 20
_TOTAL_CAP = 40
_MAX_PATHS_PER_NAME = 5
_MAX_LINES_PER_PATH = 3

_IDENTIFIER_STOPLIST = {
    "main",
    "args",
    "argv",
    "parser",
    "logger",
    "data",
    "name",
    "path",
    "test",
    "debug",
    "true",
    "false",
    "none",
}
_FLAG_STOPLIST = {
    "--help",
    "--verbose",
    "--version",
    "--debug",
    "--quiet",
    "--force",
    "--output",
    "--input",
    "--config",
    "--file",
    "--dry-run",
}
_NAME_CHARSET_RE = re.compile(r"[-A-Za-z0-9_]+")


class Candidate(NamedTuple):
    """One name a declined path dropped, and which extraction found it."""

    declined_path: str
    category: str
    name: str


def warn(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _show(repo: Path, sha: str, path: str) -> str | None:
    """PATH's content at SHA, or None when it does not exist there."""
    done = subprocess.run(
        ["git", "-C", str(repo), "show", f"{sha}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout if done.returncode == 0 else None


def _module_level_identifiers(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _cli_flags(tree: ast.AST) -> set[str]:
    flags: set[str] = set()
    for node in ast.walk(tree):
        is_add_argument = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        )
        if not is_add_argument:
            continue
        for arg in node.args:
            if (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and arg.value.startswith("--")
            ):
                flags.add(arg.value)
    return flags


def _filter_identifiers(names: set[str]) -> set[str]:
    return {
        n
        for n in names
        if len(n) >= 4
        and not n.startswith("_")
        and n.lower() not in _IDENTIFIER_STOPLIST
    }


def _filter_flags(names: set[str]) -> set[str]:
    return {
        n
        for n in names
        if n not in _FLAG_STOPLIST and len(n) >= 5 and _NAME_CHARSET_RE.fullmatch(n)
    }


def _dropped_names(
    repo: Path, base_sha: str, merge_sha: str, path: str
) -> tuple[set[str], set[str]]:
    """(identifiers, flags) present at BASE_SHA and gone at MERGE_SHA for PATH.

    Empty on either side when a blob is missing there — that is not a parse
    failure, just nothing to compare. A syntax error on either side warns and
    yields nothing: a half-parsed diff would misattribute drops.
    """
    base_blob, merge_blob = _show(repo, base_sha, path), _show(repo, merge_sha, path)
    if base_blob is None or merge_blob is None:
        return set(), set()
    try:
        base_tree, merge_tree = ast.parse(base_blob), ast.parse(merge_blob)
    except SyntaxError as exc:
        warn(f"::warning::dropped-name-seams: {path} does not parse ({exc}) — skipping")
        return set(), set()
    dropped_ids = _module_level_identifiers(base_tree) - _module_level_identifiers(
        merge_tree
    )
    dropped_flags = _cli_flags(base_tree) - _cli_flags(merge_tree)
    return _filter_identifiers(dropped_ids), _filter_flags(dropped_flags)


def _grep(
    repo: Path, merge_sha: str, pattern: str, exclude_path: str, word: bool
) -> list[str]:
    """`sha:path:line:content` lines in the merged tree matching PATTERN,
    outside EXCLUDE_PATH. [] on no match; a warning and [] on a grep error —
    exit 1 (no match) must never be read as a seam."""
    argv = ["git", "-C", str(repo), "grep", "-I", "-n"]
    if word:
        argv.append("-w")
    argv += ["-E", "-e", pattern, merge_sha, "--", f":(exclude){exclude_path}"]
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    if done.returncode > 1:
        warn(
            f"::warning::dropped-name-seams: git grep failed ({done.returncode}) "
            f"on {exclude_path}: {done.stderr.strip()}"
        )
        return []
    return [line for line in done.stdout.splitlines() if line]


def _boundary_re(name: str, exclude_hyphen: bool) -> re.Pattern[str]:
    excluded = r"[A-Za-z0-9_-]" if exclude_hyphen else r"[A-Za-z0-9_]"
    return re.compile(rf"(?<!{excluded})" + re.escape(name) + rf"(?!{excluded})")


def _attribute(
    lines: list[str], names: list[str], exclude_hyphen: bool
) -> dict[str, dict[str, list[int]]]:
    """NAME -> referencing path -> line numbers, each name matched against
    its own boundary-anchored pattern so a combined grep batch's hits are
    split back out."""
    patterns = {n: _boundary_re(n, exclude_hyphen) for n in names}
    hits: dict[str, dict[str, list[int]]] = {n: {} for n in names}
    for line in lines:
        parts = line.split(":", 3)
        if len(parts) < 4:
            continue
        _sha, ref_path, lineno, content = parts
        for name, pattern in patterns.items():
            if not pattern.search(content):
                continue
            paths = hits[name]
            if ref_path not in paths and len(paths) >= _MAX_PATHS_PER_NAME:
                continue
            linenos = paths.setdefault(ref_path, [])
            if len(linenos) < _MAX_LINES_PER_PATH:
                linenos.append(int(lineno))
    return {n: p for n, p in hits.items() if p}


def _format_line(name: str, declined_path: str, hits: dict[str, list[int]]) -> str:
    clauses = [
        f"`{ref_path}` (lines {', '.join(str(n) for n in linenos)})"
        for ref_path, linenos in hits.items()
    ]
    return f"- `{name}` — dropped from `{declined_path}`; still referenced by {', '.join(clauses)}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Report a dropped name whose merged-clean caller still calls it."
    )
    parser.add_argument("--merge", required=True, help="the merge commit's SHA")
    parser.add_argument(
        "--base", required=True, help="the pre-merge SHA that had the name"
    )
    parser.add_argument(
        "--repo", type=Path, default=None, help="the checkout (default: cwd)"
    )
    parser.add_argument("declined_paths", nargs="+", metavar="path")
    args = parser.parse_args(argv)
    repo = args.repo or Path.cwd()

    candidates: list[Candidate] = []
    for path in args.declined_paths:
        if not path.endswith(".py"):
            continue
        ids, flags = _dropped_names(repo, args.base, args.merge, path)
        ids_list = sorted(ids)[:_PER_FILE_CATEGORY_CAP]
        if len(ids) > _PER_FILE_CATEGORY_CAP:
            warn(
                f"::warning::dropped-name-seams: {path} dropped {len(ids)} identifiers; "
                f"reporting the first {_PER_FILE_CATEGORY_CAP}"
            )
        flags_list = sorted(flags)[:_PER_FILE_CATEGORY_CAP]
        if len(flags) > _PER_FILE_CATEGORY_CAP:
            warn(
                f"::warning::dropped-name-seams: {path} dropped {len(flags)} flags; "
                f"reporting the first {_PER_FILE_CATEGORY_CAP}"
            )
        candidates += [Candidate(path, "identifier", n) for n in ids_list]
        candidates += [Candidate(path, "flag", n) for n in flags_list]

    if len(candidates) > _TOTAL_CAP:
        warn(
            f"::warning::dropped-name-seams: {len(candidates)} dropped names found; "
            f"reporting the first {_TOTAL_CAP}"
        )
        candidates = candidates[:_TOTAL_CAP]

    grouped: dict[tuple[str, str], list[str]] = {}
    for candidate in candidates:
        grouped.setdefault((candidate.declined_path, candidate.category), []).append(
            candidate.name
        )

    output: list[str] = []
    for (path, category), names in grouped.items():
        exclude_hyphen = category == "flag"
        pattern = (
            f"(^|[^[:alnum:]_-])({'|'.join(names)})([^[:alnum:]_-]|$)"
            if exclude_hyphen
            else "|".join(names)
        )
        lines = _grep(repo, args.merge, pattern, path, word=not exclude_hyphen)
        hits = _attribute(lines, names, exclude_hyphen)
        for name in names:
            if name in hits:
                output.append(_format_line(name, path, hits[name]))

    if output:
        print("\n".join(output))


if __name__ == "__main__":
    main()
