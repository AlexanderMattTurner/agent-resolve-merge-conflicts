""".github/resolver/auto-resolve/hook-py-specs.py — the pin extraction that provisions
the auto-resolve job's pre-commit interpreter.

The behaviour that matters is that a pin the resolver needs cannot go missing
silently: a dropped dependency has to fail HERE, naming the remedy, rather than as a
ModuleNotFoundError inside a hook that the resolver then reads as a failed conflict
resolution.
"""

# covers: .pre-commit-config.yaml
# covers: .github/resolver/auto-resolve/install-hook-tools.sh

import ast
import functools
import importlib.util
import itertools
import re
import sys
from pathlib import Path

import pytest
import yaml

from tests._resolver_helpers import REPO_ROOT

_SRC = REPO_ROOT / ".github" / "resolver" / "auto-resolve" / "hook-py-specs.py"
_spec = importlib.util.spec_from_file_location("hook_py_specs", _SRC)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _pyproject(tmp_path: Path, dev: list[str], runtime: list[str] | None = None) -> str:
    body = ", ".join(f'"{d}"' for d in dev)
    runtime_body = ", ".join(f'"{d}"' for d in runtime or [])
    p = tmp_path / "pyproject.toml"
    p.write_text(
        f'[project]\nname = "x"\nversion = "0"\ndependencies = [{runtime_body}]\n'
        f"[project.optional-dependencies]\ndev = [{body}]\n",
        encoding="utf-8",
    )
    return str(p)


def test_returns_only_the_wanted_pins_sorted(tmp_path: Path) -> None:
    # Driven from WANTED so a new member is covered the day it is added; `pytest` is
    # the unwanted pin that must not come back out.
    wanted = [f"{name}==1.0" for name in sorted(mod.WANTED)]
    path = _pyproject(tmp_path, ["pytest==9.0.3", *reversed(wanted)])
    assert mod.dev_specs(path) == wanted


@pytest.mark.parametrize("dropped", sorted(mod.WANTED))
def test_a_missing_pin_is_reported_by_name_and_the_rest_still_install(
    tmp_path: Path, dropped: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # Member by member: each name is a separate way the provisioning can go quiet,
    # and a check that only covered one would pass while the others regressed.
    #
    # Reported, not fatal: WANTED is the union over every caller's hooks, and a
    # caller that uses no hook needing this one simply does not pin it. What must
    # not happen is silence — the name has to reach stderr, and every OTHER pin
    # has to still be installed.
    kept = [f"{name}==1.0" for name in sorted(mod.WANTED) if name != dropped]
    selected = mod.dev_specs(_pyproject(tmp_path, kept))
    assert dropped in capsys.readouterr().err
    assert selected == kept


def test_a_name_matches_regardless_of_case_separator_and_extras(tmp_path: Path) -> None:
    # Case, `-`/`_`/`.` separators and extras/markers are all legal respellings of the
    # same distribution under PEP 503, so normalizing is what stops an edit that
    # changes none of the versions reading as a dropped pin.
    path = _pyproject(
        tmp_path,
        [
            "PyYAML==6.0.3",
            "tree_sitter[extra]==0.26.0",
            "Tree.Sitter-Bash>=0.25.1",
            "TREE_SITTER_JAVASCRIPT==0.25.0",
            "PathSpec==1.1.1",
        ],
    )
    assert mod.dev_specs(path) == [
        "PathSpec==1.1.1",
        "PyYAML==6.0.3",
        "tree_sitter[extra]==0.26.0",
        "Tree.Sitter-Bash>=0.25.1",
        "TREE_SITTER_JAVASCRIPT==0.25.0",
    ]


def test_runtime_specs_read_the_dependencies_table_not_the_dev_extra(
    tmp_path: Path,
) -> None:
    # The two modes must read different tables: a runtime read that fell back to the
    # dev extra would install nothing and let the redactor stay missing, which is the
    # silent placeholder this provisioning exists to remove.
    path = _pyproject(
        tmp_path,
        dev=["pyyaml==6.0.3", "tree-sitter==0.26.0", "tree-sitter-bash==0.25.1"],
        runtime=["httpx==0.28.1", "agent-sanitizer[secrets]==2.7.2"],
    )
    assert mod.runtime_specs(path) == ["agent-sanitizer[secrets]==2.7.2"]


@pytest.mark.parametrize("dropped", sorted(mod.RUNTIME_WANTED))
def test_a_missing_runtime_pin_is_reported_by_name(
    tmp_path: Path, dropped: str, capsys: pytest.CaptureFixture[str]
) -> None:
    kept = [f"{name}==1.0" for name in sorted(mod.RUNTIME_WANTED) if name != dropped]
    selected = mod.runtime_specs(_pyproject(tmp_path, dev=[], runtime=kept))
    assert dropped in capsys.readouterr().err
    assert selected == kept


def test_a_near_miss_distribution_name_does_not_satisfy_a_runtime_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `agent-sanitizer-extras` is a DIFFERENT distribution that a prefix match would
    # accept, installing something whose import the post-condition then rejects.
    # It must satisfy nothing: the selection stays empty and the real name is
    # reported as unpinned.
    selected = mod.runtime_specs(
        _pyproject(tmp_path, dev=[], runtime=["agent-sanitizer-extras==1.0"])
    )
    assert selected == []
    assert "agent-sanitizer" in capsys.readouterr().err


# `yaml` is the one import name that is not its own distribution name; every other
# third-party import here canonicalizes to its distribution under PEP 503.
_IRREGULAR_DISTRIBUTIONS = {"yaml": "pyyaml"}
_SCRIPTS = REPO_ROOT / ".github" / "scripts"
_INTERPRETERS = ("python", "python3")


def _hook_entry_scripts() -> list[Path]:
    """Every .py a `language: system` pre-commit hook runs through the ambient
    interpreter.

    Those hooks get no environment from pre-commit, so their imports must resolve
    against the interpreter the auto-resolve job provisions. A `language: python`
    hook stays out: pre-commit builds it a virtualenv from its own
    `additional_dependencies`, so the ambient interpreter never has to satisfy it.
    A `uv run … python x.py` entry stays out for the same reason, since uv supplies
    the dev extra. The interpreter is matched anywhere in the entry, not only as its
    first word: a hook wrapping it in `bash -c` reaches the same one.
    """
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text("utf-8"))
    found = []
    for repo in config["repos"]:
        for hook in repo.get("hooks", []):
            words = [w.strip("'\"") for w in str(hook.get("entry", "")).split()]
            if hook.get("language") != "system" or words[:1] == ["uv"]:
                continue
            for word, script in itertools.pairwise(words):
                if word not in _INTERPRETERS or not script.endswith(".py"):
                    continue
                path = REPO_ROOT / script
                # A path that does not resolve would be walked as zero files, so the
                # derivation below would silently stop covering that hook.
                assert path.is_file(), f"hook {hook['id']} runs a missing {path}"
                found.append(path)
    return found


def _declared_hook_modules() -> set[str]:
    """The import names install-hook-tools.sh asserts after pip installs them.

    That post-install loop is the half of the provisioning that proves the
    interpreter can import what it was given; a name absent from the array is
    pip-installed and never checked.
    """
    installer = (
        REPO_ROOT / ".github" / "resolver" / "auto-resolve" / "install-hook-tools.sh"
    ).read_text("utf-8")
    match = re.search(
        r"^_HOOK_PY_MODULES=\((?P<modules>[^)]*)\)", installer, re.MULTILINE
    )
    # Not a soft read: an unreadable array would derive the empty set, and every
    # assertion resting on it would then pass over nothing.
    assert match, "cannot read _HOOK_PY_MODULES from install-hook-tools.sh"
    declared = set(match.group("modules").split())
    assert declared, "_HOOK_PY_MODULES is empty"
    return declared


def _fold_path(node: ast.expr, env: dict[str, Path], importer: Path) -> Path | None:
    """NODE as a directory, over the `sys.path` expressions this tree writes.

    `__file__` folds to IMPORTER, a bare name to whatever an earlier assignment
    bound, and `repo_root(...)` to the root. `.parents[N]` folds like a run of
    `.parent`, because the generator scripts here spell it that way. Anything else
    folds to None, so an expression this cannot read drops a directory instead of
    inventing one.
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
        if base and isinstance(index, ast.Constant) and type(index.value) is int:
            # Bounded by the folded directory's own depth: `parents` raises past it,
            # and an index this cannot place drops the directory like any other
            # expression it cannot read.
            if index.value < len(base.parents):
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
        if called == "repo_root":
            return REPO_ROOT
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
def _sys_path_roots(importer: Path) -> tuple[Path, ...]:
    """The directories IMPORTER itself puts on `sys.path`, in source order.

    A script run through the ambient interpreter reaches a module outside its own
    directory by inserting that directory first, so a resolver that knew only the
    siblings reads such a module as a distribution and demands a pin nobody can add.
    Pure by construction — it reads only IMPORTER — which is what makes the cache
    keyed on that path sound.
    """
    env: dict[str, Path] = {}
    roots: list[Path] = []
    for statement in ast.parse(importer.read_text("utf-8")).body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            folded = _fold_path(statement.value, env, importer)
            if isinstance(target, ast.Name) and folded:
                env[target.id] = folded
        elif isinstance(statement, ast.Expr) and _is_sys_path_call(statement.value):
            call = statement.value
            assert isinstance(call, ast.Call)
            folded = _fold_path(call.args[-1], env, importer) if call.args else None
            if folded:
                roots.append(folded)
    return tuple(roots)


def _local_files(name: str, importer: Path) -> list[Path]:
    """NAME's files when IMPORTER can reach it without installing a distribution.

    A package resolves to every module under it, not just its `__init__.py`: a hook
    importing one submodule reaches whatever that submodule imports.
    """
    for parent in (importer.parent, _SCRIPTS, *_sys_path_roots(importer)):
        if (parent / f"{name}.py").is_file():
            return [parent / f"{name}.py"]
        if (parent / name / "__init__.py").is_file():
            return sorted((parent / name).rglob("*.py"))
    return []


def _walk_imports(roots: list[Path]) -> tuple[set[str], set[Path]]:
    """The non-stdlib top-level modules ROOTS import, and every file the walk read.

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
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for top in (name.split(".")[0] for name in names):
                local = _local_files(top, path)
                if local:
                    queue.extend(local)
                elif top not in sys.stdlib_module_names:
                    third_party.add(top)
    return third_party, seen


def _synthetic_hook(tmp_path: Path, insert: str) -> tuple[Path, Path]:
    """A hook script reaching `zzhelper` ONLY through INSERT, and that module's file.

    `zzhelper` sits one directory above the importer's own, so a lookup limited to
    the siblings and to `.github/scripts` cannot reach it; `zzhelper` itself imports
    a real third-party name, so a walk that stops there loses a pin.
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    helper = lib / "zzhelper.py"
    helper.write_text("import yaml\n", encoding="utf-8")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    script = hooks / "zzcheck.py"
    script.write_text(
        f"import os\nimport sys\nfrom pathlib import Path\n\n{insert}\n\n"
        "import zzhelper\n",
        encoding="utf-8",
    )
    return script, helper


@pytest.mark.parametrize(
    "insert",
    [
        'sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))',
        'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))',
        'sys.path.append(str(Path(__file__).resolve().parent.parent / "lib"))',
        '_LIB = Path(__file__).parent.parent / "lib"\nsys.path.insert(0, str(_LIB))',
    ],
)
def test_a_module_behind_a_sys_path_insert_resolves_local(
    tmp_path: Path, insert: str
) -> None:
    # The behaviour the walk exists for. Without the sys.path lookup `zzhelper` is
    # a top-level name no directory holds, so it is reported as a distribution the
    # pyproject must pin, and the `yaml` inside it is never reached at all.
    script, helper = _synthetic_hook(tmp_path, insert)
    imports, visited = _walk_imports([script])
    assert imports == {"yaml"}
    assert helper in visited


@pytest.mark.parametrize(
    "insert",
    [
        'sys.path.insert(0, f"{Path(__file__).parent.parent}/lib")',
        'sys.path.insert(0, os.environ["ZZ_LIB"])',
        "sys.path.insert(0, str(Path(__file__).parent.parent / DIRNAME))",
        "sys.path.insert()",
    ],
)
def test_a_sys_path_expression_the_folder_cannot_read_yields_no_root(
    tmp_path: Path, insert: str
) -> None:
    # Dropping the directory is the safe direction: the walk then reports `zzhelper`
    # as a name it could not place, which is loud, rather than resolving it against
    # some directory the expression never named.
    script, helper = _synthetic_hook(tmp_path, insert)
    assert _sys_path_roots(script) == ()
    imports, visited = _walk_imports([script])
    assert imports == {"zzhelper"}
    assert helper not in visited


def test_wanted_covers_every_third_party_import_the_system_hooks_reach() -> None:
    """WANTED is a literal because hook-py-specs.py runs before its own dependencies
    exist, so nothing at run time can derive it. This is where it gets derived: a new
    third-party import anywhere in a `language: system` hook's module graph — including
    a transitive one, which is how tree_sitter_javascript reached the resolver and
    aborted every hook run as a failed conflict resolution — fails here by name.

    This repo's own system hooks reach no third-party import today, so the two
    membership assertions guard the first one that does. What is live here is the
    walk: it must find hooks and it must still follow a local import.
    """
    scripts = _hook_entry_scripts()
    assert scripts, (
        "read no `language: system` python hooks — the derivation below would be "
        "vacuous"
    )
    imports, visited = _walk_imports(scripts)
    assert visited > set(scripts), (
        f"walked {len(scripts)} hook scripts and followed no local import, so a "
        "third-party name reached through one would go unseen"
    )
    reached = {
        _IRREGULAR_DISTRIBUTIONS.get(name, mod._canonical(name)) for name in imports
    }
    assert reached <= mod.WANTED, (
        f"{sorted(reached - mod.WANTED)} reach a `language: system` hook but are not in "
        "WANTED, so the auto-resolve job's interpreter cannot import them — add each to "
        "WANTED in hook-py-specs.py and its IMPORT name to _HOOK_PY_MODULES in "
        "install-hook-tools.sh"
    )
    # The second half of the same edit. A name pip-installed but absent from the
    # array is never asserted importable, which is the post-install check the
    # installer's header calls load-bearing.
    declared = _declared_hook_modules()
    assert imports <= declared, (
        f"{sorted(imports - declared)} reach a `language: system` hook but are not in "
        "_HOOK_PY_MODULES in install-hook-tools.sh, so the post-install check never "
        "proves the interpreter can import them"
    )
