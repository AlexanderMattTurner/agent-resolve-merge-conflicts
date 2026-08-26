""".github/resolver/auto-resolve/hook-py-specs.py — the pin extraction that provisions
the auto-resolve job's pre-commit interpreter.

The behaviour that matters is that a pin the resolver needs cannot go missing
silently: a dropped dependency has to fail HERE, naming the remedy, rather than as a
ModuleNotFoundError inside a hook that the resolver then reads as a failed conflict
resolution.
"""

# covers: .pre-commit-config.yaml
# covers: .github/resolver/auto-resolve/install-hook-tools.sh

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests._resolver_helpers import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts" / "checks"))

from _bash_ast import (  # noqa: E402  # pylint: disable=wrong-import-position
    command_words,
    parse as parse_bash,
    walk as walk_bash,
)
from _py_imports import (  # noqa: E402  # pylint: disable=wrong-import-position
    interpreter_scripts,
    sys_path_roots,
    walk_imports,
)

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


def test_canonical_prints_the_distribution_names_the_installer_matches_on(
    tmp_path: Path,
) -> None:
    """`--canonical` is how install-hook-tools.sh asks which distributions a pip
    run installed, so the answer must survive every legal respelling — including
    the whitespace a shell copy of this normalization got wrong."""
    path = _pyproject(
        tmp_path,
        ["PyYAML >= 6.0.3", "tree_sitter[extra]==0.26.0", "PathSpec == 1.1.1"],
    )
    done = subprocess.run(
        [sys.executable, str(_SRC), "--canonical", path],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.splitlines() == ["pathspec", "pyyaml", "tree-sitter"]


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
_SHELL_SUFFIXES = (".sh", ".bash")


def _interpreter_scripts(words: list[str | None], root: Path, label: str) -> list[Path]:
    """The .py files WORDS names on an interpreter word, resolved under ROOT."""
    found = []
    for script in interpreter_scripts(words):
        path = root / script
        assert path.is_file(), f"{label} runs a missing {path}"
        found.append(path)
    return found


def _shell_scripts(shell: Path, root: Path) -> list[Path]:
    """The .py files the shell script at SHELL runs through an interpreter.

    A `language: system` hook whose entry is `bash x.sh` reaches the ambient
    interpreter one level down, so a walk that read only the entry would cover
    none of what that script runs. A word an expansion decides is read as None
    and drops out, so `python3 -m py_compile "$f"` names no script — correctly,
    since py_compile never imports what it compiles.
    """
    found = []
    for node in walk_bash(parse_bash(shell.read_text("utf-8"))):
        words = command_words(node)
        if words:
            found.extend(_interpreter_scripts(words, root, str(shell)))
    return found


def _hook_entry_scripts(config_path: Path) -> list[Path]:
    """Every .py that a hook declared `language: system` in CONFIG_PATH names on an
    interpreter word, resolved against the directory holding that config.

    Those hooks get no environment from pre-commit, so their imports must resolve
    against the interpreter the auto-resolve job provisions. A `language: python`
    hook stays out: pre-commit builds it a virtualenv from its own
    `additional_dependencies`, so the ambient interpreter never has to satisfy it.
    A `uv run … python x.py` entry stays out for the same reason, since uv supplies
    the dev extra. The interpreter is matched anywhere in the entry, not only as its
    first word: a hook wrapping it in `bash -c` reaches the same one, and a `.sh`
    entry is read for the interpreters IT runs. An entry naming a .py that no
    interpreter word claimed raises rather than contributing nothing. A remote repo
    declaring `language: system` in its own `.pre-commit-hooks.yaml` names no path
    in this tree, so it stays outside what this reads.
    """
    root = config_path.parent
    config = yaml.safe_load(config_path.read_text("utf-8"))
    found = []
    for repo in config["repos"]:
        for hook in repo.get("hooks", []):
            words = [w.strip("'\"") for w in str(hook.get("entry", "")).split()]
            if hook.get("language") != "system":
                continue
            if any(
                word == "uv" and after == "run" for word, after in zip(words, words[1:])
            ):
                continue
            before = len(found)
            found.extend(_interpreter_scripts(words, root, f"hook {hook['id']}"))
            for word in words:
                if word.endswith(_SHELL_SUFFIXES) and (root / word).is_file():
                    found.extend(_shell_scripts(root / word, root))
            # An entry naming a .py that no interpreter word claimed is the same
            # zero-file walk, reached through a spelling this cannot read. Loud
            # here, because every assertion below would pass over that hook.
            assert len(found) > before or not any(w.endswith(".py") for w in words), (
                f"hook {hook['id']} names a .py behind an interpreter this cannot read"
            )
    return found


def _declared_hook_modules() -> set[str]:
    """The import names install-hook-tools.sh asserts after pip installs them.

    That post-install loop is the half of the provisioning that proves the
    interpreter can import what it was given; a name absent from the array is
    pip-installed and never checked. Each array entry is
    `import-name:distribution`, because the loop matches the distribution the
    caller pinned and then demands the import it provides.
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
    declared = {entry.split(":", 1)[0] for entry in match.group("modules").split()}
    assert declared, "_HOOK_PY_MODULES is empty"
    return declared


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
    imports, visited = walk_imports([script])
    assert imports == {"yaml"}
    assert helper in visited


@pytest.mark.parametrize(
    "insert",
    [
        'sys.path.insert(0, f"{Path(__file__).parent.parent}/lib")',
        'sys.path.insert(0, os.environ["ZZ_LIB"])',
        'sys.path.insert(0, str(Path(__file__).resolve().parents[-1] / "lib"))',
        '_LIB = Path(__file__).parent.parent / "lib"\n'
        '_LIB = os.environ["ZZ_LIB"]\n'
        "sys.path.insert(0, str(_LIB))",
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
    assert sys_path_roots(script) == ()
    imports, visited = walk_imports([script])
    assert imports == {"zzhelper"}
    assert helper not in visited


def test_the_declared_hook_modules_are_import_names() -> None:
    """The array entries are `import-name:distribution`, and the guard below
    compares its members against import names a module graph reached. Reading a
    whole entry as one name makes both memberships unsatisfiable, so the first
    hook to import one of these fails naming an array that declares it."""
    declared = _declared_hook_modules()
    assert all(name.isidentifier() for name in declared), declared


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
    scripts = _hook_entry_scripts(REPO_ROOT / ".pre-commit-config.yaml")
    assert scripts, (
        "read no `language: system` python hooks — the derivation below would be "
        "vacuous"
    )
    imports, visited = walk_imports(scripts)
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


def _selection_config(tmp_path: Path, hooks: str) -> Path:
    """A pre-commit config holding HOOKS, beside every .py and .sh it names."""
    for name in ("plain.py", "optioned.py", "wrapped.py", "venv.py", "generated.py"):
        (tmp_path / name).write_text("import zzabsent\n", encoding="utf-8")
    for name in ("absolute.py", "versioned.py", "inner.py", "hidden.py"):
        (tmp_path / name).write_text("import zzabsent\n", encoding="utf-8")
    (tmp_path / "wrap.sh").write_text(
        '#!/usr/bin/env bash\npython3 inner.py "$1"\npython3 -m py_compile "$1"\n',
        encoding="utf-8",
    )
    config = tmp_path / ".pre-commit-config.yaml"
    config.write_text(f"repos:\n  - repo: local\n    hooks:\n{hooks}", encoding="utf-8")
    return config


def _hook(entry: str, language: str = "system") -> str:
    return (
        f"      - id: {entry.split()[-1]}\n"
        f"        language: {language}\n"
        f"        entry: {entry}\n"
    )


def test_hook_selection_reads_every_system_hook_and_no_other(tmp_path: Path) -> None:
    # Member by member over the ways an entry can hide its script or offer one the
    # ambient interpreter never runs. A hook this misses is walked as zero files,
    # and the coverage test below stays green over the rest.
    config = _selection_config(
        tmp_path,
        _hook("python3 plain.py")
        + _hook("python3 -I -u optioned.py")
        + _hook('bash -c "python3 wrapped.py"')
        + _hook("/usr/bin/python3 absolute.py")
        + _hook("python3.12 versioned.py")
        + _hook("bash wrap.sh")
        + _hook("python venv.py", language="python")
        + _hook("uv run --extra dev python generated.py")
        + _hook('bash -c "uv run python generated.py"')
        + _hook("zizmor"),
    )
    assert set(_hook_entry_scripts(config)) == {
        tmp_path / "plain.py",
        tmp_path / "optioned.py",
        tmp_path / "wrapped.py",
        tmp_path / "absolute.py",
        tmp_path / "versioned.py",
        tmp_path / "inner.py",
    }


def test_an_entry_naming_a_py_behind_an_unreadable_interpreter_raises(
    tmp_path: Path,
) -> None:
    # The loud half of the same rule. A spelling this cannot read must fail here,
    # because a hook contributing zero files leaves every assertion below passing
    # over it — the silent missing pin the derivation exists to prevent.
    config = _selection_config(tmp_path, _hook("zzwrapper hidden.py"))
    with pytest.raises(AssertionError, match="behind an interpreter this cannot read"):
        _hook_entry_scripts(config)
