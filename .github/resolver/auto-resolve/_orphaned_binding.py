"""PROBLEM CLASS — the merge kept one parent's private binding and the other
parent's readers, so the name has no reader left.

Both sides renamed the same thing in opposite directions. On agent-glovebox
#5568 the branch aliased `_sleep = time.sleep`, pointed its one wait at the
alias, and repointed two test fixtures at it; `main` kept the plain
`time.sleep(...)` call. The merge kept `main`'s call and the branch's alias, so
`_sleep` had zero readers, both fixtures patched a name nothing reads, and six
cases sat out a real cooldown until pytest-timeout killed each one.

Every line of that file traced to a parent, so the per-line provenance passes
(`_out_of_conflict`, `_neither_side`) had nothing to say, `git show
--remerge-diff` reported no delta, and the self-review passed. Only running the
tests found it.

Reporting is restricted to a module-level name spelled with a LEADING
UNDERSCORE. That underscore is the author's statement that no other module
reads the name, so zero readers in its own file means zero readers anywhere. A
public name is read by importers this analysis cannot see.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    git,
    git_result,
)

# Five names is what a reviewer opens the file for, matching `_neither_side`.
_NAMES_SHOWN = 5


def _parse(text: str) -> ast.Module | None:
    """TEXT as a module, or None when it does not parse.

    A side of a merge is unparseable in the ordinary case — it still carries
    conflict markers, or it is a Python file mid-port — and this analysis has
    nothing to say about one, so it declines rather than raising."""
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def private_module_bindings(tree: ast.Module) -> set[str]:
    """Every module-level `_name = ...` target in TREE.

    A tuple target, a subscript and an attribute are all skipped: none of them
    binds a plain module-level name that a reader could have orphaned."""
    names: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id.startswith("_"):
                names.add(target.id)
    return names


def names_read(tree: ast.Module) -> set[str]:
    """Every name TREE reads.

    A `Name` in a load context is the direct read. A string literal counts too,
    because `monkeypatch.setattr(mod, "_sleep", ...)` and `__all__` reach a
    binding by its spelling, and a name a test patches has a reader."""
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            read.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            read.add(node.value)
    return read


def orphaned_bindings(
    base: str | None, ours: str | None, theirs: str | None, resolved: str
) -> list[str]:
    """The private module-level names the RESOLVED text binds and never reads,
    that BASE did not bind, and that the parent introducing each one did read.

    The last clause is what makes this the merge's defect rather than the
    author's: a name its own parent never read was already dead when it
    arrived, and reporting it would blame the resolution for the branch."""
    resolved_tree = _parse(resolved)
    if resolved_tree is None:
        return []
    base_tree = _parse(base) if base is not None else None
    base_bound = private_module_bindings(base_tree) if base_tree else set()
    unread = private_module_bindings(resolved_tree) - names_read(resolved_tree)
    parents = [_parse(side) for side in (ours, theirs) if side is not None]
    read_by_a_parent: set[str] = set()
    for parent in parents:
        if parent is None:
            continue
        read_by_a_parent |= private_module_bindings(parent) & names_read(parent)
    return sorted((unread - base_bound) & read_by_a_parent)


def describe(names: list[str]) -> str:
    """NAMES as the list `land` renders, truncated with a count so one mangled
    resolution cannot fill a pull-request comment."""
    shown = ", ".join(names[:_NAMES_SHOWN])
    rest = len(names) - _NAMES_SHOWN
    return f"{shown}, and {rest} more" if rest > 0 else shown


def _blob(rev: str, name: str) -> str | None:
    """The text of NAME at REV, or None when REV does not carry that path.

    An absent path is the ordinary answer here — one side adds a file, or the
    merge base predates it — so this reads the exit status rather than raising."""
    done = git_result("show", f"{rev}:{name}")
    return None if done.returncode != 0 else done.stdout


def bindings_the_merge_orphaned(
    head: str, base: str, paths: list[str]
) -> dict[str, str]:
    """PATH -> the name list of its private module-level bindings the merge of
    HEAD and BASE left with no reader.

    Only a `.py` path is read: this analysis is Python's grammar. A path absent
    from the worktree was deleted by the resolution and has nothing to read."""
    merge_base = git("merge-base", head, base).strip()
    found: dict[str, str] = {}
    for name in sorted(paths):
        if not name.endswith(".py") or not Path(name).is_file():
            continue
        names = orphaned_bindings(
            _blob(merge_base, name),
            _blob(head, name),
            _blob(base, name),
            Path(name).read_text(encoding="utf-8"),
        )
        if names:
            found[name] = describe(names)
    return found


class OrphanedBindingReport:
    """The APPLICATION of the analysis above to one bundle step.

    A mixin for the reason `NeitherSideReport` is one: every method reads the
    step's own resolved set and the two parents it merged."""

    def report_bindings_the_merge_orphaned(self) -> None:
        """Name every private binding the resolution left unread, and hand the
        list to `land` so auto-merge goes off.

        Run over the tree as it will be COMMITTED, after the hooks and the
        post-merge repair pass, for the reason `report_lines_from_neither_side`
        runs there. Deferred, modify/delete and declined paths are excluded on
        the same grounds."""
        gated = (
            set(self.allowed)
            - set(self.deferred)
            - set(self.modify_delete)
            - set(self.declined)
        )
        if not gated:
            return
        found = bindings_the_merge_orphaned(
            self.checked_out_head, self.merge_base_side, sorted(gated)
        )
        for name, names in sorted(found.items()):
            self.orphaned_bindings.append(f"{name}\t{names}")
            print(
                f"::warning::the resolution left {names} in '{name}' with no "
                "reader, and a parent that introduced one of these names did "
                "read it. The merge is landing with auto-merge off so a human "
                "reads it first."
            )
