"""The slow-run sidecar's upload/download pair, and the boundary it keeps.

PROBLEM CLASS — a diagnostic file uploaded under a name nothing downloads is
worse than no upload at all: it reads as fixed while `land` never sees it. The
two ends have to name the SAME artifact, and the pushable `merge.bundle` upload
must keep excluding a cancelled run — a bundle a kill left half-written must
never reach `land`.

# covers: .github/workflows/auto-resolve.yaml
"""

import yaml

from tests._helpers import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-resolve.yaml"
SIDECAR_ARTIFACT = "auto-resolve-slow-run-${{ inputs.pr }}"


def _steps(job_name: str) -> list[dict]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return [step for step in doc["jobs"][job_name]["steps"] if isinstance(step, dict)]


def _named(steps: list[dict], name: str) -> dict:
    matches = [step for step in steps if step.get("name") == name]
    assert matches, f"{WORKFLOW.name}'s job has no step named {name!r}"
    return matches[0]


def test_the_merge_bundle_upload_still_excludes_a_cancelled_run() -> None:
    step = _named(_steps("resolve"), "Upload the resolved merge for the land job")
    assert step.get("if") == "!cancelled()", (
        "the pushable merge.bundle must never reach `land` from a run GitHub "
        "killed mid-write."
    )
    assert step["with"]["name"] == "auto-resolve-merge-${{ inputs.pr }}"


def test_the_slow_run_sidecar_uploads_unconditionally_under_its_own_name() -> None:
    step = _named(_steps("resolve"), "Upload the slow-run sidecar for the land job")
    assert step.get("if") == "always()", (
        "a run GitHub killed at timeout-minutes is exactly the run the slow-run "
        "advisory exists for, so this upload must not exclude it."
    )
    assert step["with"]["name"] == SIDECAR_ARTIFACT


def test_land_downloads_the_same_sidecar_artifact_name_the_resolve_job_uploads() -> (
    None
):
    upload = _named(_steps("resolve"), "Upload the slow-run sidecar for the land job")
    download = _named(_steps("land"), "Download the slow-run sidecar")
    assert download["with"]["name"] == upload["with"]["name"] == SIDECAR_ARTIFACT
    # Same directory as the merge-bundle download, so land.sh's plain `-f` checks
    # against BUNDLE_DIR see both artifacts' files without a second env var.
    merge_download = _named(_steps("land"), "Download the resolved merge")
    assert download["with"]["path"] == merge_download["with"]["path"]
