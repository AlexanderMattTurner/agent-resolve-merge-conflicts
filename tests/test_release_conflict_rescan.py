"""The step that re-scans open pull requests after a release pushes.

PROBLEM CLASS — a step that calls a privileged API from a job whose token lacks
the scope, or that names a target which does not accept the call. Both answer
only on a real release, after the tags are already pushed, and the symptom is
silence: the pull requests the release conflicted simply wait for the 6-hourly
cron, which is the delay this step exists to remove.
"""

# covers: .github/workflows/release-tags.yaml

import yaml

from tests._helpers import REPO_ROOT

RESCAN_STEP_NAME = "Re-scan open pull requests the release may have conflicted"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-tags.yaml"
LABELER = REPO_ROOT / ".github" / "workflows" / "pr-meta-privileged.yaml"


def _release_job() -> dict:
    doc = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    return doc["jobs"]["release"]


def _rescan_step() -> dict:
    steps = [
        step
        for step in _release_job()["steps"]
        if isinstance(step, dict) and step.get("name") == RESCAN_STEP_NAME
    ]
    assert len(steps) == 1, (
        f"{RELEASE_WORKFLOW.name} must carry exactly one '{RESCAN_STEP_NAME}' "
        f"step; found {len(steps)}."
    )
    return steps[0]


def test_the_release_job_may_dispatch_a_workflow() -> None:
    """`gh workflow run` answers HTTP 403 without this scope, and the release
    has already pushed its tags by the time the step runs."""
    assert _release_job()["permissions"].get("actions") == "write", (
        f"the release job runs '{RESCAN_STEP_NAME}', so it needs "
        "`actions: write`; without it the dispatch 403s on every release."
    )


def test_the_rescan_runs_only_when_a_release_actually_pushed() -> None:
    """A dry run, an unchanged version and a failed push all push nothing, so
    there is nothing for the sweep to find — `released` is the one fact that
    separates them, and the dry-run variable is not."""
    assert _rescan_step()["if"] == "steps.release.outputs.released == 'true'"


def test_the_labeler_accepts_the_dispatch_this_step_sends() -> None:
    """A target that stopped taking `workflow_dispatch`, or a `label` job whose
    `if` refuses that event, makes the step a no-op that reports success."""
    doc = yaml.safe_load(LABELER.read_text(encoding="utf-8"))
    # PyYAML reads the bare `on:` key as the boolean True.
    triggers = doc[True]
    assert "workflow_dispatch" in triggers, (
        f"{LABELER.name} must accept workflow_dispatch: release-tags.yaml "
        "dispatches it after every release."
    )
    assert "workflow_dispatch" in doc["jobs"]["label"]["if"], (
        f"{LABELER.name}'s `label` job must admit workflow_dispatch, or the "
        "dispatched run scans nothing."
    )


def test_a_dispatched_sweep_polls_like_a_base_push() -> None:
    """Querying is what starts GitHub's lazy mergeability computation, and this
    repository's labeler has no merge-tree fallback: a pull request still
    UNKNOWN after the passes only earns a warning and waits for the cron. The
    release dispatch stands in for the base push, so it takes that budget —
    only the cron keeps the cheap one."""
    doc = yaml.safe_load(LABELER.read_text(encoding="utf-8"))
    step = next(
        step
        for step in doc["jobs"]["label"]["steps"]
        if isinstance(step, dict) and "MAX_PASSES" in step.get("env", {})
    )
    passes = step["env"]["MAX_PASSES"]
    assert "github.event_name == 'schedule' && '2'" in passes, (
        "only the cron backstop takes the cheap 2-pass budget"
    )
    assert "|| '2' }}" not in passes, (
        "a dispatched sweep must not fall through to the cron's 2-pass budget"
    )
