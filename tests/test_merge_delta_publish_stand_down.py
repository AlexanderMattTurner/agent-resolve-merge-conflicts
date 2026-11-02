"""`publish` stands down on a head a newer push superseded, and only then.

covers: .github/workflows/merge-delta-review.yaml

The tests execute the stand-down step's own shell body with a stub `gh` that
answers the pull request's live head, and read the `current` output it writes.
"""

import os
import pathlib
import subprocess

import pytest
import yaml

REPO_ROOT = pathlib.Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
)
WORKFLOW = REPO_ROOT / ".github/workflows/merge-delta-review.yaml"
OLD, NEW = "a" * 40, "b" * 40


def _run_stand_down(
    tmp_path: pathlib.Path, *, live: str, reviewed: str, result: str, dispatched: str
) -> str:
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["publish"][
        "steps"
    ]
    body = next(s for s in steps if s.get("id") == "current")["run"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\necho {live}\n", encoding="utf-8")
    gh.chmod(0o755)
    output = tmp_path / "out"
    output.touch()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "GH_REPO": "o/r",
        "PR": "1",
        "GITHUB_OUTPUT": str(output),
        "REVIEWED": reviewed,
        "REVIEW_RESULT": result,
        "DISPATCHED": dispatched,
    }
    subprocess.run(["bash", "-e", "-c", body], env=env, check=True, timeout=30)
    return output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("live", "reviewed", "result", "dispatched", "expected"),
    [
        # A newer push cancelled this run's review after it had already
        # rendered the newer head: superseded, so stand down.
        (NEW, NEW, "cancelled", OLD, "current=false\n"),
        # Cancelled before the render, with the head moved on: also superseded.
        (NEW, "", "cancelled", OLD, "current=false\n"),
        # Cancelled on the head it was dispatched for (a timeout): report it.
        (OLD, OLD, "cancelled", OLD, "current=true\n"),
        # A failed review of the live head is a real red, whatever the dispatch.
        (NEW, NEW, "failure", OLD, "current=true\n"),
    ],
    ids=[
        "cancelled-after-render",
        "cancelled-before-render",
        "timeout-on-live",
        "failed-on-live",
    ],
)
def test_stand_down(tmp_path, live, reviewed, result, dispatched, expected):
    assert (
        _run_stand_down(
            tmp_path, live=live, reviewed=reviewed, result=result, dispatched=dispatched
        )
        == expected
    )
