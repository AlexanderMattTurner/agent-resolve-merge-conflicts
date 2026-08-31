"""The resolver clone checks out what it was pinned to, or says why it cannot.

covers: .github/workflows/merge-delta-review.yaml

Two properties, both of the step's own shell body, which the tests execute:

- Every ref form the step accepts reaches the checkout. `clone --no-tags` brings
  no tag and only the default branch's local ref, so a tag or another branch
  arrives only through the fetch's destination refspec.
- A ref that resolves nowhere is refused with its cause. `resolver-ref` empty
  means the ref is the workflow's own commit, which a fork carries and the
  default repository does not; a supplied one is the caller's own.

The step is reached through git's `insteadOf` rewrite against a local
repository, so the assertions are on what the workflow runs.
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


def _git(*args: str, cwd: pathlib.Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _seed_upstream(path: pathlib.Path) -> dict[str, str]:
    """A repository carrying each ref form the step promises to accept."""
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", ".", cwd=path)
    for sub in ("resolver", "scripts", "prompts"):
        (path / ".github" / sub).mkdir(parents=True)
        (path / ".github" / sub / "keep").write_text("x", encoding="utf-8")
    _git("add", "-A", cwd=path)
    _git("commit", "-qm", "seed", cwd=path)
    refs = {"sha": _git("rev-parse", "HEAD", cwd=path)}
    _git("tag", "v9.9.9", cwd=path)
    refs["tag"] = "v9.9.9"
    # A branch that is NOT the default, which is the form a plain clone misses.
    _git("checkout", "-q", "-b", "side", cwd=path)
    _git("commit", "-qm", "side", "--allow-empty", cwd=path)
    refs["branch"] = "side"
    refs["branch_sha"] = _git("rev-parse", "HEAD", cwd=path)
    _git("checkout", "-q", "main", cwd=path)
    return refs


def _run_step(
    tmp_path: pathlib.Path, repo: str, ref: str, ref_input: str = ""
) -> subprocess.CompletedProcess:
    """Run the step with `https://github.com/` rewritten into `tmp_path`."""
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    subprocess.run(
        [
            "git",
            "config",
            "--global",
            f"url.{tmp_path}/.insteadOf",
            "https://github.com/",
        ],
        check=True,
        env=env,
        capture_output=True,
    )
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", _resolver_step_body()],
        capture_output=True,
        text=True,
        env={
            **env,
            "RUNNER_TEMP": str(runner_temp),
            "RESOLVER_REPO": repo,
            "RESOLVER_REF": ref,
            "RESOLVER_REF_INPUT": ref_input,
            "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        },
    )


@pytest.fixture(name="upstream")
def _upstream(tmp_path: pathlib.Path) -> dict[str, str]:
    return _seed_upstream(tmp_path / "up" / "stream.git")


def test_the_pinned_commit_is_checked_out_and_its_paths_published(
    tmp_path: pathlib.Path, upstream: dict[str, str]
) -> None:
    """The ordinary call. Asserting the exact HEAD and the exact output text is
    what a body that checked out the default branch instead would fail."""
    result = _run_step(tmp_path, "up/stream", upstream["sha"])

    assert result.returncode == 0, result.stderr
    dest = tmp_path / "runner" / "resolver"
    assert _git("rev-parse", "HEAD", cwd=dest) == upstream["sha"]
    assert (tmp_path / "outputs").read_text(encoding="utf-8") == (
        f"dir={dest}/.github/resolver\n"
        f"scripts={dest}/.github/scripts\n"
        f"prompts={dest}/.github/prompts\n"
    )


@pytest.mark.parametrize("form", ["tag", "branch"])
def test_a_tag_and_a_non_default_branch_both_reach_the_checkout(
    tmp_path: pathlib.Path, upstream: dict[str, str], form: str
) -> None:
    """`clone --no-tags` carries neither, so each arrives only through the fetch's
    destination refspec. Both are forms `resolver-ref` documents accepting."""
    result = _run_step(tmp_path, "up/stream", upstream[form], ref_input=upstream[form])

    assert result.returncode == 0, result.stderr
    want = upstream["sha"] if form == "tag" else upstream["branch_sha"]
    assert _git("rev-parse", "HEAD", cwd=tmp_path / "runner" / "resolver") == want


def test_an_unresolvable_default_ref_is_refused_as_a_fork_mismatch(
    tmp_path: pathlib.Path, upstream: dict[str, str]
) -> None:
    """An EMPTY `resolver-ref` means the ref came from this workflow's own commit,
    so the repository named is the one a fork forgot to change."""
    absent = "0" * 40

    result = _run_step(tmp_path, "up/stream", absent)

    assert result.returncode != 0
    assert "::error::" in result.stderr
    assert "up/stream" in result.stderr
    assert absent in result.stderr
    assert "resolver-repository" in result.stderr


def test_an_unresolvable_supplied_ref_is_not_blamed_on_a_fork(
    tmp_path: pathlib.Path, upstream: dict[str, str]
) -> None:
    """The caller named this ref, so telling them to change `resolver-repository`
    would be wrong advice — a typo in `resolver-ref` reads the same otherwise."""
    result = _run_step(tmp_path, "up/stream", "no-such-ref", ref_input="no-such-ref")

    assert result.returncode != 0
    assert "resolver-ref input named" in result.stderr
    assert "resolver-repository" not in result.stderr
