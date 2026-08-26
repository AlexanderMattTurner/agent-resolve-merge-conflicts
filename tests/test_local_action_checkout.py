"""A job that uses a LOCAL composite action (`uses: ./…`) needs the repository
on disk first, or the runner never finds the action's `action.yaml` and the job
dies at "Prepare all required actions" before any step runs.

That is a silent inertness, not a noisy one: the workflow still appears in the
checks list, and only the run's log says the job never started. `claude.yaml`'s
`@claude` responder shipped that way, so every mention failed in ~1 second.

Derived from the workflow directory rather than from a list, so a job added
later is covered the day it lands.
"""

from pathlib import Path

import pytest
import yaml

from tests._helpers import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _jobs_using_a_local_action() -> list[tuple[str, str]]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_name, job in (workflow.get("jobs") or {}).items():
            steps = job.get("steps") or []
            if any(str(step.get("uses", "")).startswith("./") for step in steps):
                found.append((path.name, job_name))
    return found


def _steps(workflow_file: str, job_name: str) -> list[dict]:
    workflow = yaml.safe_load((WORKFLOWS / workflow_file).read_text(encoding="utf-8"))
    return workflow["jobs"][job_name].get("steps") or []


def test_the_sweep_finds_the_jobs_it_is_meant_to_cover():
    """Non-vacuity: a glob or parse that quietly matched nothing would make every
    case below pass without checking anything."""
    jobs = _jobs_using_a_local_action()
    assert len(jobs) >= 4, f"only found {jobs}"
    assert ("claude.yaml", "claude") in jobs


@pytest.mark.parametrize(
    ("workflow_file", "job_name"),
    _jobs_using_a_local_action(),
    ids=lambda value: str(value),
)
def test_a_job_checks_out_before_it_uses_a_local_action(workflow_file, job_name):
    """RED on claude.yaml before its checkout step: the job resolved
    `./.github/actions/claude-run` against an empty workspace."""
    first_local = None
    for index, step in enumerate(_steps(workflow_file, job_name)):
        uses = str(step.get("uses", ""))
        if uses.startswith("./") and first_local is None:
            first_local = index
        if uses.startswith("actions/checkout@"):
            assert first_local is None, (
                f"{workflow_file}:{job_name} uses a local action at step "
                f"{first_local} before checking out at step {index}"
            )
            return
    assert first_local is None, (
        f"{workflow_file}:{job_name} uses "
        f"{_steps(workflow_file, job_name)[first_local]['uses']} "
        "but never runs actions/checkout"
    )


def test_every_workflow_file_parses():
    """The sweep silently skips a file it cannot parse, which would hide a job."""
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict), path
    assert list(WORKFLOWS.glob("*.y*ml")), "no workflows found"


def test_the_workflow_directory_is_where_this_test_thinks_it_is():
    assert WORKFLOWS.is_dir(), WORKFLOWS
    assert Path(WORKFLOWS / "claude.yaml").is_file()
