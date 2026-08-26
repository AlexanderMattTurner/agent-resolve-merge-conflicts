"""What a Python script imports, resolved the way the ambient interpreter does.

PROBLEM CLASS — "which in-repo files does this Python entry point need on
disk". A script run through the ambient interpreter (``python3 path/to/x.py``,
no package, no virtualenv) reaches a module through its own directory and
through whatever directories it inserts on ``sys.path`` itself. A caller that
resolves imports against the repo root instead — the way pytest does — reads
``from _lockfiles import ...`` as a third-party name and drops the file. Every
caller that must know a script's local files asks here, so the answer has one
definition: the hook-pin extractor (``tests/test_hook_py_specs.py``) and the
sparse-checkout closure check both read this.

The walk counts EVERY import, including one inside a function body: the file
must be on disk whenever that code path runs. A caller that wants
collection-time imports only wants ``.github/scripts/pytest-import-closure.py``,
which answers that different question against the repo root.

Nothing here imports or executes what it reads — parsing is enough, and a
caller may run on a bare runner with no dependencies installed.
"""

import ast
import functools
import sys
from pathlib import Path


def _fold_path(node: ast.expr, env: dict[str, Path], importer: Path) -> Path | None:
    """NODE as a directory, over the `sys.path` expressions this tree writes.

    `__file__` folds to IMPORTER and a bare name to whatever an earlier assignment
    bound. `.parents[N]` folds like a run of `.parent`, because the generator
    scripts here spell it that way. Anything else folds to None, so an expression
    this cannot read drops a directory instead of inventing one.
    """
    if isinstance(node, ast.Name):
        return importer if node.id == "__file__" else env.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _fold_path(node.value, env, importer)
        return base.parent if base else None
    if isinstance(node, ast.Subscript):
        index = node.slice
        base = (
            _fold_path(node.value.value, env, importer)
            if isinstance(node.value, ast.Attribute) and node.value.attr == "parents"
            else None
        )
        # Bounded at both ends by the folded directory's own depth: `parents`
        # raises past it and reads a negative index from the other end, so an index
        # this cannot place drops the directory like any expression it cannot read.
        if base and isinstance(index, ast.Constant) and type(index.value) is int:
            if 0 <= index.value < len(base.parents):
                return base.parents[index.value]
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        base = _fold_path(node.left, env, importer)
        right = node.right
        if base and isinstance(right, ast.Constant) and isinstance(right.value, str):
            return base / right.value
        return None
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "resolve":
            return _fold_path(func.value, env, importer)
        called = func.id if isinstance(func, ast.Name) else ""
        if called in ("str", "Path") and node.args:
            return _fold_path(node.args[0], env, importer)
    return None


def _is_sys_path_call(node: ast.expr) -> bool:
    """True for a `sys.path.insert(...)` or `sys.path.append(...)` call."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in ("insert", "append"):
        return False
    path_attr = node.func.value
    return (
        isinstance(path_attr, ast.Attribute)
        and path_attr.attr == "path"
        and isinstance(path_attr.value, ast.Name)
        and path_attr.value.id == "sys"
    )


@functools.cache
def sys_path_roots(importer: Path) -> tuple[Path, ...]:
    """The directories IMPORTER itself puts on `sys.path`, in source order.

    A script run through the ambient interpreter reaches a module outside its own
    directory by inserting that directory first, so a resolver that knew only the
    siblings reads such a module as a distribution and demands a pin nobody can add.
    Pure in its arguments — it reads only IMPORTER — which is what makes the cache
    sound. The key is the path, not the file's contents, so a caller that rewrites
    a script it already asked about gets the first parse back.
    """
    env: dict[str, Path] = {}
    roots: list[Path] = []
    for statement in ast.parse(importer.read_text("utf-8")).body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            folded = _fold_path(statement.value, env, importer)
            if isinstance(target, ast.Name):
                # A rebinding this cannot read CLEARS the name. Leaving the earlier
                # binding standing would resolve a later insert against a directory
                # the current expression never named.
                env.pop(target.id, None)
                if folded:
                    env[target.id] = folded
        elif isinstance(statement, ast.Expr) and _is_sys_path_call(statement.value):
            call = statement.value
            assert isinstance(call, ast.Call)
            folded = _fold_path(call.args[-1], env, importer) if call.args else None
            if folded:
                roots.append(folded)
    return tuple(roots)


def local_files(name: str, importer: Path) -> list[Path]:
    """NAME's files when IMPORTER can reach it without installing a distribution.

    Only the directories the ambient interpreter really searches: the script's own,
    which Python puts on `sys.path` for it, and the ones the script declares. A
    directory no importer named would resolve a name that IS a distribution, and
    the missing pin then passes as local. A package resolves to every module under
    it, not just its `__init__.py`: a hook importing one submodule reaches whatever
    that submodule imports.
    """
    for parent in (importer.parent, *sys_path_roots(importer)):
        if (parent / f"{name}.py").is_file():
            return [parent / f"{name}.py"]
        if (parent / name / "__init__.py").is_file():
            return sorted((parent / name).rglob("*.py"))
    return []


def walk_imports(roots: list[Path]) -> tuple[set[str], set[Path]]:
    """The non-stdlib top-level modules ROOTS import, and every file the walk read.

    Every import counts, including one inside a function body: the interpreter must
    satisfy it whenever that path runs. `.github/scripts/pytest-import-closure.py`
    answers a different question — what executes at COLLECTION time — so it reads
    module-level imports only, and reusing it here would drop a pin the hook needs.

    The second half is what lets a caller assert the walk actually followed a local
    import: a walk that stopped resolving would report an empty set of third-party
    names and read as a clean pass.
    """
    seen, queue, third_party = set(), list(roots), set()
    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for top in (name.split(".")[0] for name in names):
                local = local_files(top, path)
                if local:
                    queue.extend(local)
                elif top not in sys.stdlib_module_names:
                    third_party.add(top)
    return third_party, seen
