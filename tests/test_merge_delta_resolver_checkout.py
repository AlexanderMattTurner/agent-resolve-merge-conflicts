"""The renderer clone refuses a ref the named repository does not carry.

covers: .github/workflows/merge-delta-review.yaml

`resolver-ref` defaults to `job.workflow_sha`, the commit of the repository the
workflow itself came from. `resolver-repository` defaults to a literal naming
upstream. A fork that ships this workflow and leaves the repository at its
default therefore clones upstream and asks for a commit only the fork has. The
refusal names that mismatch, because `git checkout` alone says only
"reference is not a tree".

The step body is executed for real, against a local repository reached through
git's own `insteadOf` rewrite, so the assertion is on what the workflow runs.
"""

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


def _resolver_step_body() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["review"]["steps"]
    return next(s for s in steps if s.get("id") == "resolver")["run"]


def _git(*args: str, cwd: pathlib.Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _seed_repo(path: pathlib.Path, marker: str) -> str:
    """A repository holding the three directories the step publishes.

    `marker` keeps two seeded repositories at DIFFERENT commits: git hashes the
    tree and the timestamp, so identical content in the same second collides.
    """
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", ".", cwd=path)
    for sub in ("resolver", "scripts", "prompts"):
        (path / ".github" / sub).mkdir(parents=True)
        (path / ".github" / sub / "keep").write_text(marker, encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "seed", cwd=path)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def _run_step(body: str, tmp_path: pathlib.Path, repo: str, ref: str):
    """Run the step with `https://github.com/` rewritten into `tmp_path`."""
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(
        [
            "git",
            "config",
            "--global",
            f"url.{tmp_path}/.insteadOf",
            "https://github.com/",
        ],
        check=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
    )
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", body],
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "RUNNER_TEMP": str(runner_temp),
            "RESOLVER_REPO": repo,
            "RESOLVER_REF": ref,
            "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        },
    )


@pytest.fixture(name="upstream_sha")
def _upstream_sha(tmp_path: pathlib.Path) -> str:
    return _seed_repo(tmp_path / "up" / "stream.git", "upstream")


def test_a_commit_the_named_repository_carries_is_checked_out(
    tmp_path: pathlib.Path, upstream_sha: str
) -> None:
    """The ordinary call: the pin resolves, and the step publishes its paths."""
    result = _run_step(_resolver_step_body(), tmp_path, "up/stream", upstream_sha)

    assert result.returncode == 0, result.stderr
    outputs = (tmp_path / "outputs").read_text(encoding="utf-8")
    assert "dir=" in outputs and "scripts=" in outputs and "prompts=" in outputs


def test_a_forks_own_commit_is_refused_by_name_against_upstream(
    tmp_path: pathlib.Path, upstream_sha: str
) -> None:
    """A fork's commit is absent upstream, and the refusal says which repository
    lacked it and that the fork must name itself in `resolver-repository`."""
    fork_sha = _seed_repo(tmp_path / "fork" / "clone.git", "fork")
    assert fork_sha != upstream_sha

    result = _run_step(_resolver_step_body(), tmp_path, "up/stream", fork_sha)

    assert result.returncode != 0
    assert "::error::" in result.stderr
    assert "up/stream" in result.stderr
    assert fork_sha in result.stderr
    assert "resolver-repository" in result.stderr
