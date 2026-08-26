"""template-sync's resolve step must run the BASE ref's driver script.

The step checks out the `template-sync` branch before resolving, so the tree it
runs in carries the template's freshly synced copy of every synced script. Two
things go wrong when the step calls that copy. It executes template code no one
has reviewed, which the token-scope step above it refuses to do by design. And
in a repo that relocated a synced library, the template's copy sources a path
this tree no longer has: run 32714126287 died on `RESOLVER_DIR required` and run
33013873426 on a missing `.github/scripts/lib/shared-names.bash`, both before
one conflicted file was resolved, leaving PR #53 stuck with raw markers.

The test runs the step's own shell body against a sandbox whose two copies of
the driver are distinguishable, and asserts which one ran.
"""

import subprocess
from pathlib import Path

import yaml

from tests._helpers import REPO_ROOT, commit_files, git_env, init_test_repo

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "template-sync.yaml"
DRIVER = ".github/scripts/template-sync-resolve.sh"
STEP_NAME = "Resolve the sync conflicts"


def _step_body(name: str) -> str:
    """The `run:` block of the named step, as GitHub would execute it."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    bodies = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == name
    ]
    assert len(bodies) == 1, f"expected one {name!r} step, found {len(bodies)}"
    return bodies[0]


def _driver(marks: str) -> str:
    """A stand-in driver that records which copy of itself ran."""
    return f'#!/usr/bin/env bash\nprintf "{marks}" >"$MARKER"\n'


def test_the_resolve_step_runs_the_base_copy_of_the_driver(tmp_path: Path):
    repo = tmp_path / "child"
    init_test_repo(repo)
    base_sha = commit_files(repo, {DRIVER: _driver("base")}, "base")
    env = git_env()
    subprocess.run(
        ["git", "checkout", "-q", "-b", "template-sync"], cwd=repo, check=True
    )
    commit_files(
        repo, {DRIVER: _driver("branch"), "conflicted.txt": "<<<<<<<\n"}, "sync"
    )
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    # The step fetches the sync branch from `origin`; here the repo is its own.
    subprocess.run(
        ["git", "remote", "add", "origin", "."], cwd=repo, env=env, check=True
    )

    marker = tmp_path / "which-driver"
    run = subprocess.run(
        ["bash", "-e", "-c", _step_body(STEP_NAME)],
        cwd=repo,
        env={
            **env,
            "RUNNER_TEMP": str(tmp_path / "runner-temp"),
            "GITHUB_SHA": base_sha,
            "MARKER": str(marker),
        },
        capture_output=True,
        text=True,
    )

    assert run.returncode == 0, run.stderr
    assert marker.read_text(encoding="utf-8") == "base"
    # The other half of the step: the driver reads the SYNC BRANCH's files, so
    # the working directory must be on that branch when it runs.
    assert (repo / "conflicted.txt").read_text(encoding="utf-8") == "<<<<<<<\n"
