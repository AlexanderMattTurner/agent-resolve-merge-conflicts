"""template-sync's resolve step must run the BASE ref's driver script.

The step checks out the `template-sync` branch before resolving, so the tree it
runs in carries the template's freshly synced copy of every synced script. Two
things go wrong when the step calls that copy. It executes template code no one
has reviewed, which the token-scope step above it refuses to do by design. And
in a repo that relocated a synced library, the template's copy sources a path
this tree no longer has: run 32714126287 died on `RESOLVER_DIR required` and run
33013873426 on a missing `.github/scripts/lib/shared-names.bash`, which this
repo moved under `.github/resolver/`. Both died before one conflicted file was
resolved, leaving PR #53 carrying raw markers.

The test runs the two steps' own shell bodies against a sandbox whose two copies
of the driver are distinguishable, and asserts which one ran.
"""

import subprocess
from pathlib import Path

import yaml

from tests._helpers import REPO_ROOT, commit_files, git_env, init_test_repo

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "template-sync.yaml"
DRIVER = ".github/scripts/template-sync-resolve.sh"
PIN_STEP = "Pin the base ref's driver scripts"
RESOLVE_STEP = "Resolve the sync conflicts"


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


def _run(body: str, repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-e", "-c", body], cwd=repo, env=env, capture_output=True, text=True
    )


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
    step_output = tmp_path / "step-output"
    step_output.touch()
    step_env = {
        **env,
        "RUNNER_TEMP": str(tmp_path / "runner-temp"),
        "GITHUB_SHA": base_sha,
        "GITHUB_OUTPUT": str(step_output),
        "MARKER": str(marker),
    }

    pin = _run(_step_body(PIN_STEP), repo, step_env)
    assert pin.returncode == 0, pin.stderr
    outputs = dict(
        line.split("=", 1)
        for line in step_output.read_text(encoding="utf-8").splitlines()
    )
    # GitHub binds this from the pin step's output; the body reads it as an env
    # var so no step interpolates a value into its own shell.
    resolve = _run(
        _step_body(RESOLVE_STEP), repo, {**step_env, "DRIVERS_DIR": outputs["dir"]}
    )

    assert resolve.returncode == 0, resolve.stderr
    assert marker.read_text(encoding="utf-8") == "base"
    # The other half of the step: the driver reads the SYNC BRANCH's files, so
    # the working directory must be on that branch when it runs.
    assert (repo / "conflicted.txt").read_text(encoding="utf-8") == "<<<<<<<\n"
