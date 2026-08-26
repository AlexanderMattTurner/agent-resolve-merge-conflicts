"""`setup-command` prepares the caller's checkout before the model runs.

A repository may need its checkout repaired before an agent can start in it at
all: `.dotfiles` tracks `.claude/hooks` as a symlink into an ignored directory,
so the link dangles in every CI checkout and Claude Code exits on the `statx`
before it reads a prompt — on every credential rung, which reads as a dead token
rather than a broken tree.

The command is a script the PR HEAD's own tree supplies, and the resolve job
holds every model credential, so it carries the same fork gate
`pre-pass-command` does. These cases EVALUATE the shipped step's real condition
against synthetic payloads rather than grepping it for a guard string: a guard
present but wired into the wrong arm still reds.

# covers: .github/workflows/auto-resolve.yaml
"""

import subprocess

import pytest
import yaml

from tests._gha_if import evaluate
from tests._helpers import REPO_ROOT

REUSABLE = REPO_ROOT / ".github" / "workflows" / "auto-resolve.yaml"

STEP = "Prepare the merged tree for the model"
LADDER = "Resolve the remaining source conflicts with Claude"

REPO = "owner/repo"
FORK = "outsider/repo"

# The same condition WITHOUT the fork gate. The fork case below is decided
# against it too, so a payload the shipped condition refuses is one this
# spelling admits — which is what proves the gate, and not some unrelated
# clause, is doing the refusing.
UNGUARDED = (
    "inputs.setup-command != '' && (steps.prepare.outputs.needs_llm == 'true' "
    "|| steps.prepare.outputs.needs_commit == 'true')"
)


def _step(name: str) -> dict:
    doc = yaml.safe_load(REUSABLE.read_text(encoding="utf-8"))
    steps = {s["name"]: s for s in doc["jobs"]["resolve"]["steps"] if "name" in s}
    assert name in steps, f"{name} is gone from the resolve job"
    return steps[name]


def _context(command: str, head_repo: str, needs_llm: str, needs_commit: str) -> dict:
    return {
        "github": {"repository": REPO},
        "inputs": {"setup-command": command},
        "steps": {
            "prepare": {
                "outputs": {"needs_llm": needs_llm, "needs_commit": needs_commit}
            },
            "selected": {"outputs": {"head_repo": head_repo}},
        },
    }


@pytest.mark.parametrize(
    ("command", "head_repo", "needs_llm", "needs_commit", "runs", "why"),
    [
        ("bash prep.sh", REPO, "true", "", True, "a named command on a same-repo head"),
        ("bash prep.sh", REPO, "", "true", True, "the self-review path alone"),
        ("bash prep.sh", FORK, "true", "true", False, "a fork head executes nothing"),
        ("", REPO, "true", "true", False, "an empty command runs nothing"),
        ("bash prep.sh", REPO, "", "", False, "no model call to prepare for"),
    ],
)
def test_the_condition_decides_who_gets_prepared(
    command: str,
    head_repo: str,
    needs_llm: str,
    needs_commit: str,
    runs: bool,
    why: str,
) -> None:
    context = _context(command, head_repo, needs_llm, needs_commit)
    assert evaluate(str(_step(STEP)["if"]), context) is runs, why


def test_only_the_fork_gate_refuses_a_fork_head() -> None:
    """Non-vacuity for the case that matters. The un-gated spelling ADMITS the
    same fork payload the shipped condition refuses, so the refusal above is the
    gate's doing."""
    context = _context("bash prep.sh", FORK, "true", "true")
    assert evaluate(UNGUARDED, context) is True
    assert evaluate(str(_step(STEP)["if"]), context) is False


def test_the_caller_s_command_runs_before_the_model_call() -> None:
    doc = yaml.safe_load(REUSABLE.read_text(encoding="utf-8"))
    names = [s.get("name") for s in doc["jobs"]["resolve"]["steps"]]
    assert names.index(STEP) < names.index(LADDER), (
        "the preparation runs after the fan-out, so the model still meets the "
        "checkout this step exists to repair"
    )


def test_the_run_line_executes_the_caller_s_command_verbatim(tmp_path) -> None:
    """The shipped `run:`, driven for real: the command reaches a shell with its
    own quoting intact, so a caller's `&&` and arguments survive."""
    marker = tmp_path / "prepared"
    done = subprocess.run(
        ["bash", "-c", str(_step(STEP)["run"])],
        env={
            "PATH": "/usr/bin:/bin",
            "SETUP_COMMAND": f"mkdir -p '{tmp_path}/d' && printf ok >'{marker}'",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert marker.read_text(encoding="utf-8") == "ok"


def test_the_input_is_declared_and_defaults_to_nothing() -> None:
    doc = yaml.safe_load(REUSABLE.read_text(encoding="utf-8"))
    declared = doc[True]["workflow_call"]["inputs"]["setup-command"]
    assert declared["required"] is False
    assert declared["default"] == "", (
        "an unset setup-command must run nothing, so every existing caller is "
        "unaffected"
    )
