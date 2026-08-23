"""Tests for `.github/scripts/checks/caller-command-refusal.py`.

The check exists because the resolver ran a command the CALLING repository names,
the runner could not execute it, and the raise killed the step after the model
had billed the whole resolution — twice in two days, one input apart. It answers
whether every read of such a command reaches `_refusal.run_or_refuse`.
"""

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
    """The pre-#46 shape: `check=False` catches a non-zero exit and nothing else,
    so a binary the runner lacks raises before any child exists."""
    text = _READ + "done = subprocess.run([*CMD, '--verify'], check=False)\n"
    assert check.violations(text, WANTED) == [2]


def test_a_read_that_reaches_no_refusal_at_all_is_flagged() -> None:
    """The pre-#47 shape, and the reason the check does not only look at
    `subprocess` call sites: the read is handed to a helper that runs it, so the
    unguarded call carries the helper's own parameter name instead."""
    text = _READ + "read_the_tree(CMD)\n"
    assert check.violations(text, WANTED) == [1]


def test_an_annotated_read_is_flagged_like_a_plain_one() -> None:
    """The same read wearing a type. This package already writes annotated module
    constants, so reading only `ast.Assign` would let one refactor hide a call
    site — and the check would report a vacuous clean."""
    text = (
        'CMD: list[str] = shlex.split(os.environ.get("AUTO_RESOLVE_PRE_PASS", ""))\n'
        "done = subprocess.run(CMD, check=False)\n"
    )
    assert check.violations(text, WANTED) == [2]


def test_an_argv_built_inside_the_subprocess_call_is_flagged() -> None:
    """The plainest form of the defect, and the one that binds no name: the read
    loop never sees it, so the check has to read the call's own first argument."""
    text = 'subprocess.run(shlex.split(os.environ["AUTO_RESOLVE_PRE_PASS"]))\n'
    assert check.violations(text, WANTED) == [1]


def test_an_argv_built_inside_run_or_refuse_passes() -> None:
    """The same unnamed shape, refused. Flagging it would say the one route out
    of this defect is itself the defect."""
    text = (
        'run_or_refuse(shlex.split(os.environ["AUTO_RESOLVE_PRE_PASS"]),\n'
        "    label='x', input_name='y', lost='z')\n"
    )
    assert check.violations(text, WANTED) == []


def test_a_read_routed_through_run_or_refuse_passes() -> None:
    text = _READ + "done = run_or_refuse([*CMD], label='x', input_name='y', lost='z')\n"
    assert check.violations(text, WANTED) == []


def test_a_read_of_some_other_variable_is_not_this_checks_business() -> None:
    """Only a CALLER-supplied command qualifies. A tool this tree installs itself
    has a different remedy, and flagging it would train sessions to annotate."""
    text = 'BIN = os.environ.get("MERGIRAF_BIN", "mergiraf")\nsubprocess.run([BIN])\n'
    assert check.violations(text, WANTED) == []


def test_the_annotation_needs_a_reason() -> None:
    """A blank reason is indistinguishable from a forgotten call site."""
    blank = _READ.rstrip() + "  # allow-caller-command-refusal:\n"
    assert check.violations(blank, WANTED) == [1]
    given = _READ.rstrip() + "  # allow-caller-command-refusal: a test fixture\n"
    assert check.violations(given, WANTED) == []


def test_the_variable_set_is_derived_from_the_workflow() -> None:
    """The SSOT half: both real inputs are found by name, so a third `*-command`
    input added to the workflow widens the check without touching it."""
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
    """The other half of the same closure: an input the workflow declares but
    never puts in a step's environment reaches no resolver code, so a set built
    from it would be empty and every call site would pass."""
    workflow = tmp_path / "auto-resolve.yaml"
    workflow.write_text(
        "on:\n  workflow_call:\n    inputs:\n      pre-pass-command:\n"
        "        type: string\njobs:\n  resolve:\n    steps:\n      - run: true\n",
        "utf-8",
    )
    with pytest.raises(RuntimeError, match="no env var carries"):
        check.command_env_vars(workflow)


def test_the_real_package_is_clean() -> None:
    """Dogfood, in the suite rather than only at the terminal: the check ships
    with no grandfathered baseline, so a call site added later is a red here."""
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
# (defining module, name) pairs, as `command_names` returns them: the module
# travels with the name so two package modules sharing a constant name are not
# merged into one carrier. `_refusal` is here for the module-alias case, which
# needs a module the real package defines.
_EXTERNAL = frozenset(
    {
        ("bundle", "PRE_PASS"),
        ("reader", "PRE_PASS"),
        ("_refusal", "PRE_PASS"),
    }
)


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
    text = "import _refusal\ndone = subprocess.run([*_refusal.PRE_PASS], check=False)\n"
    assert check.violations(text, WANTED, _EXTERNAL) == [2]


def test_a_bare_import_of_a_NON_PACKAGE_module_is_not_a_carrier() -> None:
    """Provenance on `import m` too: once any package module exports `PRE_PASS`,
    matching the attribute alone makes an unrelated third party's value this
    package's carrier and refuses something nobody here supplied."""
    text = (
        "import thirdparty\ndone = subprocess.run(thirdparty.PRE_PASS, check=False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == []


def test_a_KEYWORD_handoff_to_a_helper_is_flagged() -> None:
    """A helper names its own first parameter, so recognizing only `args=` reads
    `execute(argv=PRE_PASS)` as passing no command at all — and the helper's
    module then sees an ordinary parameter name."""
    text = (
        "from .bundle import PRE_PASS\n"
        "def execute(argv):\n"
        "    return subprocess.run(argv, check=False)\n"
        "execute(argv=PRE_PASS)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [4]


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
    assert check.command_names(WANTED, tmp_path) == frozenset({("reader", "PRE_PASS")})


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


def test_a_carrier_reached_through_a_relative_module_import_is_flagged() -> None:
    """`from . import bundle` binds a MODULE, not a name, so `bundle.PRE_PASS`
    reaches the carrier through an attribute the name map never sees."""
    text = "from . import bundle\ndone = subprocess.run(bundle.PRE_PASS, check=False)\n"
    assert check.violations(text, WANTED, _EXTERNAL) == [2]


def test_command_names_follows_a_carrier_through_a_RE_EXPORT(tmp_path: Path) -> None:
    """Two hops, not one. A module that re-exports a carrier under a new name
    makes that new name the SOURCE the next import sees, so a single hop of
    alias resolution leaves the second invisible and the run passes clean."""
    (tmp_path / "reader.py").write_text(
        "import os, shlex\n"
        'PRE_PASS = shlex.split(os.environ.get("AUTO_RESOLVE_PRE_PASS", ""))\n',
        "utf-8",
    )
    (tmp_path / "middle.py").write_text(
        "from .reader import PRE_PASS as COMMAND\n", "utf-8"
    )
    carriers = check.command_names(WANTED, tmp_path)
    assert carriers == frozenset({("reader", "PRE_PASS"), ("middle", "COMMAND")})
    runner = (
        "from .middle import COMMAND\ndone = subprocess.run(COMMAND, check=False)\n"
    )
    assert check.violations(runner, WANTED, carriers) == [2]


def test_command_names_follows_an_ASSIGNMENT_re_export(tmp_path: Path) -> None:
    """`COMMAND = PRE_PASS` re-exports the same value with no alias, so an
    import-only closure never reaches `COMMAND`."""
    (tmp_path / "reader.py").write_text(
        "import os, shlex\n"
        'PRE_PASS = shlex.split(os.environ.get("AUTO_RESOLVE_PRE_PASS", ""))\n',
        "utf-8",
    )
    (tmp_path / "middle.py").write_text(
        "from .reader import PRE_PASS\nCOMMAND = PRE_PASS\n", "utf-8"
    )
    carriers = check.command_names(WANTED, tmp_path)
    # `middle` re-exports under BOTH names, so an import of either reaches the
    # carrier and both pairs belong in the set.
    assert carriers == frozenset(
        {("reader", "PRE_PASS"), ("middle", "PRE_PASS"), ("middle", "COMMAND")}
    )
    runner = (
        "from .middle import COMMAND\ndone = subprocess.run(COMMAND, check=False)\n"
    )
    assert check.violations(runner, WANTED, carriers) == [2]


def test_an_imported_carrier_handed_to_a_HELPER_is_flagged() -> None:
    """The imported twin of `test_a_read_that_reaches_no_refusal_at_all`: the
    carrier binds no read line here, so the helper's own module sees only its
    parameter name and the caller's command runs raw with nobody flagged."""
    text = "from .bundle import PRE_PASS\nexecute(PRE_PASS)\n"
    assert check.violations(text, WANTED, _EXTERNAL) == [2]


def test_an_imported_carrier_that_reaches_run_or_refuse_is_clean() -> None:
    text = (
        "from .bundle import PRE_PASS\n"
        'd = run_or_refuse(PRE_PASS, label="x", input_name="y", lost="z")\n'
    )
    assert check.violations(text, WANTED, _EXTERNAL) == []


def test_a_carrier_RENAMED_inside_this_module_is_still_flagged() -> None:
    """The module that performs the rename is the one the package-wide set
    cannot help: `COMMAND` is neither an env read here nor an import, so
    matching only those two lets the rename walk past the refusal."""
    text = (
        "from .reader import PRE_PASS\n"
        "COMMAND = PRE_PASS\n"
        "done = subprocess.run(COMMAND, check=False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [3]


def test_a_rename_CHAIN_and_an_argv_rewrap_keep_carrier_status() -> None:
    """To a fixed point, and through the argv shape the package actually
    writes: `[*PRE_PASS, *args]` is the command with arguments appended."""
    chained = (
        "from .reader import PRE_PASS\n"
        "A = PRE_PASS\n"
        "B = A\n"
        "done = subprocess.run(B, check=False)\n"
    )
    assert check.violations(chained, WANTED, _EXTERNAL) == [4]
    rewrapped = (
        "from .reader import PRE_PASS\n"
        'ARGV = [*PRE_PASS, "--verify"]\n'
        "done = subprocess.run(ARGV, check=False)\n"
    )
    assert check.violations(rewrapped, WANTED, _EXTERNAL) == [3]


def test_a_value_DERIVED_from_a_carrier_is_not_one() -> None:
    """Precision, and the reason a rename is not "any expression mentioning a
    carrier": `run_or_refuse` returns a CompletedProcess, and calling that a
    command reds every `print(done.stdout)` in the real package."""
    completed = (
        "from .reader import PRE_PASS\n"
        'done = run_or_refuse(PRE_PASS, label="x", input_name="y", lost="z")\n'
        "print(done.stdout)\n"
    )
    assert check.violations(completed, WANTED, _EXTERNAL) == []
    derived = (
        "from .reader import PRE_PASS\n"
        'flag = "true" if PRE_PASS else "false"\n'
        "print(flag)\n"
    )
    assert check.violations(derived, WANTED, _EXTERNAL) == []


def test_the_args_KEYWORD_is_read_like_the_positional_argv() -> None:
    """`subprocess.run` accepts both spellings, so reading only the positional
    one lets `args=` run a caller's command with nothing flagged."""
    text = (
        "from .bundle import PRE_PASS\n"
        "done = subprocess.run(args=PRE_PASS, check=False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [2]


def test_a_LOCAL_binding_that_shadows_a_carrier_is_not_flagged() -> None:
    """A carrier's name is a module-wide string and Python is not: a parameter
    or a local assignment of the same name is a different value, and refusing it
    would refuse something the caller never supplied."""
    parameter = (
        "from .bundle import PRE_PASS\n"
        "def go(PRE_PASS):\n"
        "    return subprocess.run(PRE_PASS, check=False)\n"
    )
    assert check.violations(parameter, WANTED, _EXTERNAL) == []
    local = (
        "from .bundle import PRE_PASS\n"
        "def go(x):\n"
        "    PRE_PASS = compute(x)\n"
        "    return subprocess.run(PRE_PASS, check=False)\n"
    )
    assert check.violations(local, WANTED, _EXTERNAL) == []
    # The same name UNSHADOWED inside a function is still the carrier.
    inherited = (
        "from .bundle import PRE_PASS\n"
        "def go():\n"
        "    return subprocess.run(PRE_PASS, check=False)\n"
    )
    assert check.violations(inherited, WANTED, _EXTERNAL) == [3]


def test_a_binding_in_a_NESTED_scope_shadows_nothing_outside_it() -> None:
    """A nested function, lambda or class is its own scope, so a name it binds
    leaves the enclosing one resolving to the module-level carrier. Walking the
    whole subtree drops that carrier and reports the raw run clean."""
    nested_function = (
        "from .bundle import PRE_PASS\n"
        "def outer():\n"
        "    def inner():\n"
        "        PRE_PASS = compute()\n"
        "        return PRE_PASS\n"
        "    return subprocess.run(PRE_PASS, check=False)\n"
    )
    assert check.violations(nested_function, WANTED, _EXTERNAL) == [6]
    nested_class = (
        "from .bundle import PRE_PASS\n"
        "def outer():\n"
        "    class Holder:\n"
        "        PRE_PASS = compute()\n"
        "    return subprocess.run(PRE_PASS, check=False)\n"
    )
    assert check.violations(nested_class, WANTED, _EXTERNAL) == [5]


def test_a_helper_that_also_runs_its_parameter_RAW_is_no_refusal() -> None:
    """One branch refuses and the other runs the same parameter raw, so the
    caller's command still reaches a subprocess. Counting the helper as a
    refusal clears the handoff this check exists to catch."""
    both = (
        "from .bundle import PRE_PASS\n"
        "def execute(argv, dry):\n"
        "    if dry:\n"
        '        return run_or_refuse(argv, label="x", input_name="y", lost="z")\n'
        "    return subprocess.run(argv, check=False)\n"
        "execute(PRE_PASS, False)\n"
    )
    # Line 6 only: `argv` inside the helper is a parameter, not a carrier here.
    assert check.violations(both, WANTED, _EXTERNAL) == [6]


def test_a_third_party_symbol_of_the_same_name_is_not_a_carrier() -> None:
    """Provenance: matching the symbol name alone makes any package export turn
    an unrelated `from thirdparty import COMMAND` into the caller's command."""
    text = (
        "from thirdparty import COMMAND\ndone = subprocess.run(COMMAND, check=False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == []


def test_a_helper_that_refuses_its_own_first_parameter_clears_the_handoff() -> None:
    """`_read_the_tree(argv)` routes its parameter through `run_or_refuse`, so a
    carrier handed to it IS refused. Before scopes were tracked this held by
    accident, because the helper's parameter shared the caller's name."""
    text = (
        "import os, shlex\n"
        "def read_the_tree(argv):\n"
        '    return run_or_refuse(argv, label="x", input_name="y", lost="z")\n'
        "def run():\n"
        '    cmd = shlex.split(os.environ.get("AUTO_RESOLVE_PRE_PASS", ""))\n'
        "    return read_the_tree(cmd)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == []
    # A helper that does NOT refuse leaves the read reported.
    unguarded = text.replace(
        '    return run_or_refuse(argv, label="x", input_name="y", lost="z")\n',
        "    return subprocess.run(argv, check=False)\n",
    )
    # Reported at the READ: nothing refused it, and the read is where the
    # value came from — the same shape as a local read handed to any helper.
    assert check.violations(unguarded, WANTED, _EXTERNAL) == [5]


def test_a_SAME_NAMED_symbol_from_another_package_module_is_not_the_carrier() -> None:
    """Provenance inside the package too: `other.py` may define its own
    `PRE_PASS`, and merging it with the carrier refuses a value no caller
    supplied."""
    text = "from .other import PRE_PASS\ndone = subprocess.run(PRE_PASS, check=False)\n"
    assert check.violations(text, WANTED, _EXTERNAL) == []


def test_a_NESTED_refusal_does_not_make_the_outer_helper_refusing() -> None:
    """The nested function may never run, so its `run_or_refuse` proves nothing
    about the path the caller's command actually takes."""
    text = (
        "from .bundle import PRE_PASS\n"
        "def execute(argv):\n"
        "    def unused():\n"
        '        return run_or_refuse(argv, label="x", input_name="y", lost="z")\n'
        "    return hand_off(argv)\n"
        "execute(PRE_PASS)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [6]


def test_a_rename_of_a_SHADOWED_name_is_not_a_carrier() -> None:
    """`CMD` holds the parameter, not the caller's command, so promoting it
    module-wide reports the caller's own value as a caller-supplied command."""
    text = (
        "from .bundle import PRE_PASS\n"
        "def f(PRE_PASS):\n"
        "    CMD = PRE_PASS\n"
        "    return subprocess.run(CMD, check=False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == []


def test_a_NESTED_helper_of_the_same_name_does_not_clear_the_module_one() -> None:
    """Two `execute`s: the module-level one runs the command raw, the nested one
    refuses. Pooling helper names by string lets the nested definition clear
    every call to the definition that actually runs."""
    text = (
        "from .bundle import PRE_PASS\n"
        "def execute(argv):\n"
        "    return subprocess.run(argv, check=False)\n"
        "def outer():\n"
        "    def execute(argv):\n"
        '        return run_or_refuse(argv, label="x", input_name="y", lost="z")\n'
        "    return execute\n"
        "execute(PRE_PASS)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [8]


def test_a_MODULE_QUALIFIED_carrier_survives_a_rename() -> None:
    """`bundle.PRE_PASS` is the carrier under another spelling, so binding it to
    a name has to keep carrier status — a bare-`Name`-only rename test lets the
    module-qualified form walk past the refusal."""
    text = (
        "from . import bundle\n"
        "COMMAND = bundle.PRE_PASS\n"
        "done = subprocess.run(COMMAND, check=False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [3]


def test_a_CONCATENATED_argv_keeps_carrier_status() -> None:
    """`PRE_PASS + ["--flag"]` still runs the caller's executable, so binding it
    to a name has to stay a carrier."""
    text = (
        "from .bundle import PRE_PASS\n"
        'COMMAND = PRE_PASS + ["--flag"]\n'
        "done = subprocess.run(COMMAND, check=False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [3]


def test_a_DERIVED_module_value_is_not_a_re_export(tmp_path: Path) -> None:
    """`COUNT = len(PRE_PASS)` is an integer. Putting it in the package-wide set
    reds every downstream `print(COUNT)`."""
    (tmp_path / "reader.py").write_text(
        "import os, shlex\n"
        'PRE_PASS = shlex.split(os.environ.get("AUTO_RESOLVE_PRE_PASS", ""))\n'
        "COUNT = len(PRE_PASS)\n",
        "utf-8",
    )
    assert check.command_names(WANTED, tmp_path) == frozenset({("reader", "PRE_PASS")})


def test_a_helper_that_RENAMES_before_running_raw_is_no_refusal() -> None:
    """The raw branch renames the parameter first, so matching the parameter's
    own spelling reads that branch as touching no command and the helper is
    classified a refusal."""
    text = (
        "from .bundle import PRE_PASS\n"
        "def execute(argv, dry):\n"
        "    if dry:\n"
        '        return run_or_refuse(argv, label="x", input_name="y", lost="z")\n'
        "    command = argv\n"
        "    return subprocess.run(command, check=False)\n"
        "execute(PRE_PASS, False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [7]


def test_a_helper_whose_other_branch_hands_off_RAW_is_no_refusal() -> None:
    """The indirect form of the mixed-branch case: `execute` refuses on one
    branch and hands the parameter to `unsafe` on the other, and `unsafe` runs
    it. A helper refuses only if every target of its parameter does."""
    text = (
        "from .bundle import PRE_PASS\n"
        "def unsafe(argv):\n"
        "    return subprocess.run(argv, check=False)\n"
        "def execute(argv, safe):\n"
        "    if safe:\n"
        '        return run_or_refuse(argv, label="x", input_name="y", lost="z")\n'
        "    return unsafe(argv)\n"
        "execute(PRE_PASS, False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [8]


def test_a_helper_that_hands_off_to_a_REFUSING_helper_still_refuses() -> None:
    """The recall side of the same rule: a chain of local helpers that all end
    at `run_or_refuse` is a refusal, so the fixed point must not collapse it."""
    text = (
        "from .bundle import PRE_PASS\n"
        "def inner(argv):\n"
        '    return run_or_refuse(argv, label="x", input_name="y", lost="z")\n'
        "def execute(argv):\n"
        "    return inner(argv)\n"
        "execute(PRE_PASS)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == []


def test_two_scopes_importing_the_SAME_NAME_keep_their_own_provenance() -> None:
    """A function-local import binds in that scope alone. Collapsing both under
    the bound spelling keeps whichever the walk saw last, so the sibling's
    unrelated `PRE_PASS` decides for the one that runs the carrier raw."""
    text = (
        "def unsafe():\n"
        "    from .bundle import PRE_PASS\n"
        "    return subprocess.run(PRE_PASS, check=False)\n"
        "def unrelated():\n"
        "    from .other import PRE_PASS\n"
        "    return subprocess.run(PRE_PASS, check=False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [3]


def test_a_helper_that_hands_the_parameter_NOWHERE_refuses_nothing() -> None:
    """`all()` over an empty target list says every target refuses, which is the
    opposite of what a helper that never passes the command on does."""
    text = (
        "from .bundle import PRE_PASS\n"
        "def discard(argv):\n"
        "    return None\n"
        "discard(PRE_PASS)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [4]


def test_a_nested_helper_SHADOWING_a_refusing_one_does_not_clear_it() -> None:
    """The reverse binding of the earlier nested-helper case: the module-level
    `execute` refuses, the nested one runs raw, and a call inside that scope
    reaches the nested definition."""
    text = (
        "from .bundle import PRE_PASS\n"
        "def execute(argv):\n"
        '    return run_or_refuse(argv, label="x", input_name="y", lost="z")\n'
        "def outer():\n"
        "    def execute(argv):\n"
        "        return subprocess.run(argv, check=False)\n"
        "    return execute(PRE_PASS)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == [7]


def test_an_alias_bound_INSIDE_a_function_stays_there() -> None:
    """`CMD` in `f` is that function's. Promoting it module-wide makes an
    unrelated module-level `CMD` the caller's command."""
    text = (
        "from .bundle import PRE_PASS\n"
        "def f():\n"
        "    CMD = PRE_PASS\n"
        '    return run_or_refuse(CMD, label="x", input_name="y", lost="z")\n'
        'CMD = ["echo"]\n'
        "done = subprocess.run(CMD, check=False)\n"
    )
    assert check.violations(text, WANTED, _EXTERNAL) == []


def test_a_QUALIFIED_re_export_reaches_a_downstream_module(tmp_path: Path) -> None:
    """`COMMAND = bundle.PRE_PASS` at module level re-exports the carrier, so a
    module importing COMMAND and running it raw has to be flagged."""
    (tmp_path / "bundle.py").write_text(
        "import os, shlex\n"
        'PRE_PASS = shlex.split(os.environ.get("AUTO_RESOLVE_PRE_PASS", ""))\n',
        "utf-8",
    )
    (tmp_path / "middle.py").write_text(
        "from . import bundle\nCOMMAND = bundle.PRE_PASS\n", "utf-8"
    )
    carriers = check.command_names(WANTED, tmp_path)
    assert ("middle", "COMMAND") in carriers
    runner = (
        "from .middle import COMMAND\ndone = subprocess.run(COMMAND, check=False)\n"
    )
    assert check.violations(runner, WANTED, carriers) == [2]
