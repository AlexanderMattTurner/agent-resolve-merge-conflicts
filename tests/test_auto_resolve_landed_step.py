"""The step name a consumer's latency chart dates its series from.

PROBLEM CLASS — a step NAME read back out of the Actions API by another
repository. A consumer's chart finds the moment a resolution reached the head by
looking for one step name in this workflow's `land` job; renaming the step here
empties that series and nothing on either side reports it. The name is checked
here because both halves of the contract — the workflow step and the `land.sh`
output that gates it — live in this repository.

`agent-glovebox`'s `.github/scripts/_chart_ci_runs.py` holds the consumer half,
`LANDED_STEP_NAME`, with a comment naming this file. The OTHER half of the
contract — that a real push writes the output this step gates on — is asserted by
driving the script, in `land.test.mjs`.
"""

# covers: .github/workflows/auto-resolve.yaml

import yaml

from tests._helpers import REPO_ROOT

LANDED_STEP_NAME = "The resolution is on the branch"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-resolve.yaml"


def _landed_step() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = [
        step
        for step in doc["jobs"]["land"]["steps"]
        if isinstance(step, dict) and step.get("name") == LANDED_STEP_NAME
    ]
    assert steps, (
        f"{WORKFLOW.name}'s `land` job has no step named {LANDED_STEP_NAME!r} — a "
        "consumer's resolve-latency series is dated from that name and goes empty."
    )
    return steps[0]


def test_the_land_job_carries_the_step_the_latency_chart_dates_from() -> None:
    assert _landed_step()["if"] == "steps.land.outputs.pushed == 'true'", (
        "the step must run only on a real push: land.sh exits 0 on endings that "
        "push nothing, so an unconditional step would date a series from runs "
        "that landed no resolution."
    )
