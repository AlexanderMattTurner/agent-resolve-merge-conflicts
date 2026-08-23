"""Tests for `.github/scripts/checks/caller-command-refusal.py`.\n\nThe check exists because the resolver ran a command the CALLING repository names,\nthe runner could not execute it, and the raise killed the step after the model\nhad billed the whole resolution — twice in two days, one input apart. It answers\nwhether every read of such a command reaches `_refusal.run_or_refuse`.\n"""

import importlib.util
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

CHECK = REPO_ROOT / ".github" / "scripts" / "checks" / "caller-command-refusal.py"
WANTED = frozenset({"AUTO_RESOLVE_PRE_PASS"})
_READ = 'CMD = shlex.split(os.environ.get("AUTO_RESOLVE_PRE_PASS", ""))\n'


def _load():
    spec = importlib.util.spec_from_file_location("caller_command_refusal", CHECK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check = _load()


def test_a_read_run_straight_by_subprocess_is_flagged() -> None:
    """The pre-#46 shape: `check=False` catches a non-zero exit and nothing else,\n    so a binary the runner lacks raises before any child exists."""
    text = _READ + "done = subprocess.run([*CMD, '--verify'], check=False)\n"
    assert check.violations(text, WANTED) == [2]


def test_a_read_that_reaches_no_refusal_at_all_is_flagged() -> None:
    """The pre-#47 shape, and the reason the check does not only look at\n    `subprocess` call sites: the read is handed to a helper that runs it, so the\n    unguarded call carries the helper's own parameter name instead."""
    text = _READ + "read_the_tree(CMD)\n"
    assert check.violations(text, WANTED) == [1]


def test_an_annotated_read_is_flagged_like_a_plain_one() -> None:
    """The same read wearing a type. This package already writes annotated module\n    constants, so reading only `ast.Assign` would let one refactor hide a call\n    site — and the check would report a vacuous clean."""
    text = (
        'CMD: list[str] = shlex.split(os.environ.get("AUTO_RESOLVE_PRE_PASS", ""))\n'
        "done = subprocess.run(CMD, check=False)\n"
    )
    assert check.violations(text, WANTED) == [2]


def test_an_argv_built_inside_the_subprocess_call_is_flagged() -> None:
    """The plainest form of the defect, and the one that binds no name: the read\n    loop never sees it, so the check has to read the call's own first argument."""
    text = 'subprocess.run(shlex.split(os.environ["AUTO_RESOLVE_PRE_PASS"]))\n'
    assert check.violations(text, WANTED) == [1]


def test_an_argv_built_inside_run_or_refuse_passes() -> None:
    """The same unnamed shape, refused. Flagging it would say the one route out\n    of this defect is itself the defect."""
    text = (
        'run_or_refuse(shlex.split(os.environ["AUTO_RESOLVE_PRE_PASS"]),\n'
        "    label='x', input_name='y', lost='z')\n"
    )
    assert check.violations(text, WANTED) == []


def test_a_read_routed_through_run_or_refuse_passes() -> None:
    text = _READ + "done = run_or_refuse([*CMD], label='x', input_name='y', lost='z')\n"
    assert check.violations(text, WANTED) == []


def test_a_read_of_some_other_variable_is_not_this_checks_business() -> None:
    """Only a CALLER-supplied command qualifies. A tool this tree installs itself\n    has a different remedy, and flagging it would train sessions to annotate."""
    text = 'BIN = os.environ.get("MERGIRAF_BIN", "mergiraf")\nsubprocess.run([BIN])\n'
    assert check.violations(text, WANTED) == []


def test_the_annotation_needs_a_reason() -> None:
    """A blank reason is indistinguishable from a forgotten call site."""
    blank = _READ.rstrip() + "  # allow-caller-command-refusal:\n"
    assert check.violations(blank, WANTED) == [1]
    given = _READ.rstrip() + "  # allow-caller-command-refusal: a test fixture\n"
    assert check.violations(given, WANTED) == []


def test_the_variable_set_is_derived_from_the_workflow() -> None:
    """The SSOT half: both real inputs are found by name, so a third `*-command`\n    input added to the workflow widens the check without touching it."""
    assert check.command_env_vars() == frozenset(
        {"AUTO_RESOLVE_PRE_PASS", "AUTO_RESOLVE_POST_MERGE_CHECK"}
    )


def test_a_workflow_with_no_command_input_fails_loud(tmp_path: Path) -> None:
    """Fail closed: a rename that empties the set must not read as "nothing to
    check", which is the vacuous green this check would otherwise report."""
    workflow = tmp_path / "auto-resolve.yaml"
    workflow.write_text("on:\n  workflow_call:\n    inputs:\n      model:\n", "utf-8")
    with pytest.raises(RuntimeError, match="no `\\*-command` input"):
        check.command_env_vars(workflow)


def test_a_command_input_no_env_var_carries_fails_loud(tmp_path: Path) -> None:
    """The other half of the same closure: an input the workflow declares but\n    never puts in a step's environment reaches no resolver code, so a set built\n    from it would be empty and every call site would pass."""
    workflow = tmp_path / "auto-resolve.yaml"
    workflow.write_text(
        "on:\n  workflow_call:\n    inputs:\n      pre-pass-command:\n"
        "        type: string\njobs:\n  resolve:\n    steps:\n      - run: true\n",
        "utf-8",
    )
    with pytest.raises(RuntimeError, match="no env var carries"):
        check.command_env_vars(workflow)


def test_the_real_package_is_clean() -> None:
    """Dogfood, in the suite rather than only at the terminal: the check ships\n    with no grandfathered baseline, so a call site added later is a red here."""
    wanted = check.command_env_vars()
    external = check.command_names(wanted)
    offenders = [
        f"{path.name}:{lineno}"
        for path in sorted(check._PACKAGE.rglob("*.py"))
        for lineno in check.violations(
            path.read_text(encoding="utf-8"), wanted, external
        )
    ]
    assert offenders == []


# ── the cross-module half ────────────────────────────────────────────────────
_EXTERNAL = frozenset({"PRE_PASS"})


def test_a_carrier_imported_from_another_module_and_run_raw_is_flagged() -> None:
    """The gap the per-file check could not see: the module that READS the
    command hands it to `run_or_refuse` and looks clean, so a second module that
    imports the same name and runs it raw was in nobody's view."""
    text = (
        "from .bundle import PRE_PASS\ndone = subprocess.run(PRE_PASS, check=False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [2]


def test_the_same_import_is_clean_when_it_reaches_run_or_refuse() -> None:
    text = (
        "from .bundle import PRE_PASS\n"
        'done = run_or_refuse(PRE_PASS, label="x", input_name="y", lost="z")\n'
    )
    assert check.violations(text, WANTED, _EXTERNAL) == []


def test_a_carrier_reached_through_a_module_alias_is_flagged() -> None:
    """`import bundle` then `bundle.PRE_PASS` is the same value under another
    spelling, and the argv scan answers with the alias rather than the name
    unless it resolves the attribute."""
    text = "import bundle\ndone = subprocess.run([*bundle.PRE_PASS], check=False)\n"
    assert check.violations(text, WANTED, _EXTERNAL) == [2]


def test_a_local_name_that_merely_collides_with_a_carrier_is_not_flagged() -> None:
    """Precision, and the reason `command_names` counts only MODULE-level reads:
    a parameter named like a carrier is a different value, and flagging it would
    red every unrelated helper in the package."""
    text = "def run_it(PRE_PASS):\n    return subprocess.run(PRE_PASS, check=False)\n"
    assert check.violations(text, WANTED, _EXTERNAL) == []


def test_command_names_reads_module_level_bindings_across_the_package(
    tmp_path: Path,
) -> None:
    """The carrier set is derived from the package, not listed here — and a
    function-local read is excluded, because it cannot leave its module."""
    (tmp_path / "reader.py").write_text(
        "import os, shlex\n"
        'PRE_PASS = shlex.split(os.environ.get("AUTO_RESOLVE_PRE_PASS", ""))\n',
        "utf-8",
    )
    (tmp_path / "local.py").write_text(
        "import os, shlex\n"
        "def go():\n"
        '    argv = shlex.split(os.environ.get("AUTO_RESOLVE_PRE_PASS", ""))\n'
        "    return argv\n",
        "utf-8",
    )
    assert check.command_names(WANTED, tmp_path) == frozenset({"PRE_PASS"})


def test_a_carrier_imported_under_an_ALIAS_is_still_flagged() -> None:
    """The same value under another spelling. Matching on the bound name alone
    intersects to nothing against a carrier set holding the SOURCE name, so the
    rename fails open — a raw run of the caller's command with no refusal."""
    text = (
        "from .bundle import PRE_PASS as pre_pass\n"
        "done = subprocess.run(pre_pass, check=False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [2]


def test_an_alias_that_merely_LANDS_on_a_carrier_name_is_not_flagged() -> None:
    """The other direction of the same lookup: importing something else AS
    `PRE_PASS` binds a carrier's name to a value no `*-command` read produced."""
    text = (
        "from .other import THING as PRE_PASS\n"
        "done = subprocess.run(PRE_PASS, check=False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == []
