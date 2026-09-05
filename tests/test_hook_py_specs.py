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

from tests._resolver_helpers import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts" / "checks"))


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


def test_the_declared_hook_modules_are_import_names() -> None:
    """The array entries are `import-name:distribution`, and install-hook-tools.sh
    demands the IMPORT half after pip installs the distribution half. Reading a
    whole entry as one name would make every one of those imports fail, naming an
    array that declares it."""
    declared = _declared_hook_modules()
    assert all(name.isidentifier() for name in declared), declared
