"""The job the carry-round dispatch runs in.

PROBLEM CLASS — a step that calls a privileged API from a job whose token lacks
the scope. `resolve` holds `actions: read` so the model and the pull request's
own scripts cannot start a workflow, so `gh workflow run` answers HTTP 403
there. The dispatch is what brings a run back to install a partly-resolved
conflict set, and a 403 shows up only on a rare carry, after a paid round is
spent — so the placement is pinned here instead.
"""

# covers: .github/workflows/auto-resolve.yaml

import yaml

from tests._helpers import REPO_ROOT

CARRY_STEP_NAME = (
    "Dispatch the next round for a conflict set this window could not finish"
)
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-resolve.yaml"


def _jobs_with_the_carry_step() -> list[str]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return [
        name
        for name, job in doc["jobs"].items()
        for step in job.get("steps", [])
        if isinstance(step, dict) and step.get("name") == CARRY_STEP_NAME
    ]


def _permissions(job: str) -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return doc["jobs"][job]["permissions"]


def test_only_the_land_job_dispatches_the_carry_round() -> None:
    assert _jobs_with_the_carry_step() == ["land"], (
        f"{WORKFLOW.name} must dispatch the carry round from `land` and nowhere "
        "else: every other job's token lacks `actions: write`, so the dispatch "
        "403s and the carry chain stalls at the round that wrote the salvage."
    )


def test_the_land_job_may_start_a_workflow() -> None:
    assert _permissions("land")["actions"] == "write", (
        "the job holding the carry dispatch must be able to start a workflow."
    )


def test_the_resolve_job_may_not_start_a_workflow() -> None:
    assert _permissions("resolve")["actions"] == "read", (
        "`resolve` runs the model and the pull request's own scripts, so it must "
        "not be able to start a workflow. Widening it is not how a 403 on the "
        "carry dispatch gets fixed."
    )
