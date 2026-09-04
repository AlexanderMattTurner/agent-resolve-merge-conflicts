"""Behavior tests for _hook_gate.hooks_needing_the_project_env — the list of
pre-commit hook ids the resolve job must SKIP.

That job holds a write token and deliberately runs no `uv sync`, so a hook whose
entry runs `uv run` would let the pull request choose what the job installs and
then executes. The list is derived from the checked-out `.pre-commit-config.yaml`
rather than written beside it in the workflow, and these drive that derivation
over real config files.

# covers: .github/resolver/auto-resolve/_hook_gate.py
"""

import subprocess
import sys
import textwrap
from pathlib import Path

from tests._helpers import REPO_ROOT

MODULE_DIR = REPO_ROOT / ".github" / "resolver" / "auto-resolve"


def hooks_to_skip(
    tmp_path: Path, config: str | None, pythonpath: str = ""
) -> list[str]:
    """Run the real function in a fresh interpreter whose cwd is `tmp_path`.

    A subprocess, not an import: the module resolves its config relative to the
    process's working directory, and `pythonpath` is how a caller drives it
    under an interpreter that is missing a module.
    """
    if config is not None:
        (tmp_path / ".pre-commit-config.yaml").write_text(config, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(MODULE_DIR)!r});"
            " import _hook_gate;"
            " print('\\n'.join(_hook_gate.hooks_needing_the_project_env()))",
        ],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": pythonpath},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split()


CONFIG = textwrap.dedent("""\
    repos:
      - repo: local
        hooks:
          - id: ruff-check
            entry: uv run ruff check
          - id: shfmt
            entry: shfmt -d
      - repo: local
        hooks:
          - id: pytest-fast
            entry: uv run pytest -q
""")


def test_every_uv_run_hook_is_named_and_no_other_is(tmp_path: Path) -> None:
    """Both repo blocks are read, and a hook that does not run `uv run` stays
    runnable — refusing every hook would leave the resolution unlinted."""
    assert hooks_to_skip(tmp_path, CONFIG) == ["pytest-fast", "ruff-check"]


def test_a_repository_with_no_precommit_config_refuses_nothing(tmp_path: Path) -> None:
    """`pre-commit run` finds no hook either, so this is an empty set rather than
    a bypassed one."""
    assert hooks_to_skip(tmp_path, None) == []


def test_a_config_free_repository_needs_no_yaml_parser(tmp_path: Path) -> None:
    """PyYAML is installed by the resolve job's install-hook-tools.sh and by
    nothing else, so a calling repository that ships no pre-commit config must
    reach the empty answer without one. A module-scope `import yaml` breaks this
    and takes every `bundle.py` run in that repository with it."""
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(
        textwrap.dedent("""\
            import sys


            class _BlockYaml:
                def find_spec(self, name, path=None, target=None):
                    if name == "yaml" or name.startswith("yaml."):
                        raise ModuleNotFoundError("No module named 'yaml'")
                    return None


            sys.meta_path.insert(0, _BlockYaml())
        """),
        encoding="utf-8",
    )
    # The blocker bites, so a pass below is a run without PyYAML rather than one
    # around this fixture.
    probe = subprocess.run(
        [sys.executable, "-c", "import yaml"],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(blocker)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode != 0

    assert hooks_to_skip(tmp_path, None, pythonpath=str(blocker)) == []


WRAPPER_CONFIG = textwrap.dedent("""\
    repos:
      - repo: local
        hooks:
          - id: through-a-wrapper
            entry: bash scripts/lint.sh
          - id: through-a-plain-wrapper
            entry: bash scripts/format.sh
          - id: a-wrapper-that-only-names-it
            entry: bash scripts/lint-nouv.sh
          - id: a-regex-not-a-command
            entry: \\$\\((?:\\w+=\\S+\\s+)*retry\\s
          - id: outside-this-repository
            entry: bash /opt/vendor/lint.sh
""")


NAMES_IT_IN_A_COMMENT = textwrap.dedent("""\
    #!/usr/bin/env bash
    # `python3`, not `uv run`: the transposer imports one module the ambient
    # interpreter already carries, and `uv run` would resolve this tree's lockfile.
    exec python3 transpose.py "$@"
""")

PLAIN_WRAPPER = '#!/usr/bin/env bash\nexec shfmt -d "$@"\n'


def write_wrapper(tmp_path: Path, name: str, body: str) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / name).write_text(body, encoding="utf-8")


def test_a_wrapper_that_runs_uv_run_is_named(tmp_path: Path) -> None:
    """A hook that reaches the project environment through a script it names is one
    the resolve job must skip: reading the entry alone takes `bash scripts/lint.sh`
    for a hook that installs nothing, while its body syncs the checked-out head's
    own lockfile and then runs it."""
    write_wrapper(tmp_path, "lint.sh", "#!/usr/bin/env bash\nexec uv run ruff check\n")
    write_wrapper(tmp_path, "format.sh", PLAIN_WRAPPER)
    write_wrapper(tmp_path, "lint-nouv.sh", NAMES_IT_IN_A_COMMENT)

    assert hooks_to_skip(tmp_path, WRAPPER_CONFIG) == ["through-a-wrapper"]


def test_a_wrapper_that_only_names_it_in_a_comment_is_not_named(
    tmp_path: Path,
) -> None:
    """A comment is not a call, and the wrapper explaining why it does NOT use the
    project environment is the one a raw text search reads wrong. Skipping that hook
    leaves the merged tree unlinted by the check the comment exists to keep runnable."""
    write_wrapper(tmp_path, "lint-nouv.sh", NAMES_IT_IN_A_COMMENT)
    write_wrapper(tmp_path, "format.sh", PLAIN_WRAPPER)

    assert hooks_to_skip(tmp_path, WRAPPER_CONFIG) == []


def test_a_wrapper_that_runs_it_inside_a_quoted_command_is_named(
    tmp_path: Path,
) -> None:
    """`bash -c "uv run …"` runs it, so dropping comments must not drop a quoted
    command body with them."""
    write_wrapper(
        tmp_path, "lint.sh", '#!/usr/bin/env bash\nbash -c "uv run ruff check"\n'
    )

    assert hooks_to_skip(tmp_path, WRAPPER_CONFIG) == ["through-a-wrapper"]


def test_an_absent_or_unreadable_wrapper_names_nothing(tmp_path: Path) -> None:
    """A hook whose script this checkout does not carry must not abort the read: the
    answer covers every OTHER hook, and one missing file would refuse them all."""
    write_wrapper(tmp_path, "format.sh", PLAIN_WRAPPER)

    assert hooks_to_skip(tmp_path, WRAPPER_CONFIG) == []
