"""The merge-delta input is scoped to the pull request's OWN commits.

covers: .github/scripts/prepare-merge-delta-input.sh

The range starts at the branch the pull request merges into, not at the branch
this job checked out. A stacked pull request is where the difference shows: its
parent's merges are already answered on the parent's own pull request, and
reviewing them again both spends a paid read and raises a finding somebody
settled. The failure is silent — a wrong base reviews the wrong merges and
nothing goes red — so the assertion is on which shas the renderer covered.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
)
SCRIPT = REPO_ROOT / ".github/scripts/prepare-merge-delta-input.sh"
RESOLVER_DIR = REPO_ROOT / ".github/resolver"

# Answers the one read the script makes: which branch the pull request merges
# into. GH_BASE_REF says what to answer, so a test can drive the wrong answer too.
GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "${GH_BASE_REF}"
"""

# The real sanitizer is an npm package this checkout does not install:
# `node -e "require.resolve('agent-input-sanitizer')"` fails here. It is not the
# subject of these cases — the RANGE is — and the workflow installs it through
# install-input-sanitizer.sh before the real script runs.
SANITIZER_STUB = """process.stdin.pipe(process.stdout);
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def commit(repo: Path, path: str, text: str, message: str) -> str:
    (repo / path).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def _evil_merge(repo: Path, branch: str, marker: str) -> str:
    """Merge a conflicting side branch into BRANCH, resolving to bytes NEITHER
    parent wrote. Returns the merge sha, which the renderer must then report."""
    side = f"side-{marker}"
    git(repo, "checkout", "-q", "-b", side)
    commit(repo, "f.txt", f"one\n{marker}-THEIRS\nthree\n", f"{marker} side")
    git(repo, "checkout", "-q", branch)
    commit(repo, "f.txt", f"one\n{marker}-OURS\nthree\n", f"{marker} ours")
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", side],
        capture_output=True,
        text=True,
        check=False,
    )
    (repo / "f.txt").write_text(f"one\n{marker}-INVENTED\nthree\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-edit")
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def stack(tmp_path: Path) -> dict[str, object]:
    """A pull request stacked on another one, served from a local origin.

    `feature` is the parent's branch and carries its own hand-authored merge;
    `child` branches off it and carries a second. `refs/pull/7/head` is the
    child, which is what the script fetches.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@t")
    git(work, "config", "user.name", "t")
    commit(work, "f.txt", "one\ntwo\nthree\n", "base")

    git(work, "checkout", "-q", "-b", "feature")
    parent_merge = _evil_merge(work, "feature", "PARENT")
    git(work, "checkout", "-q", "-b", "child")
    child_merge = _evil_merge(work, "child", "CHILD")

    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "main", "feature")
    git(work, "push", "-q", "origin", "child:refs/pull/7/head")

    # The caller's own checkout: the DEFAULT branch, which is neither endpoint
    # of the range the script must derive.
    base_checkout = tmp_path / "caller"
    subprocess.run(["git", "clone", "-q", str(origin), str(base_checkout)], check=True)
    return {
        "checkout": base_checkout,
        "parent_merge": parent_merge,
        "child_merge": child_merge,
    }


def _run(stack: dict[str, object], tmp_path: Path, base_ref: str) -> list[str]:
    """Run the script with the pull request answering BASE_REF, and return the
    shas the renderer reported covering."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "gh").write_text(GH_STUB, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    scripts = tmp_path / "resolver-scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "sanitize-pr-input.mjs").write_text(SANITIZER_STUB, encoding="utf-8")

    pr_input = tmp_path / "pr-input"
    done = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=stack["checkout"],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
            "PR": "7",
            "GH_REPO": "owner/name",
            "GH_TOKEN": "t",
            "GH_BASE_REF": base_ref,
            "PR_INPUT_DIR": str(pr_input),
            "RESOLVER_DIR": str(RESOLVER_DIR),
            "RESOLVER_SCRIPTS": str(scripts),
            "GITHUB_OUTPUT": str(tmp_path / "out.txt"),
        },
    )
    assert done.returncode == 0, done.stderr
    return (pr_input / "merge-delta.shas.txt").read_text(encoding="utf-8").split()


def test_the_range_holds_the_pull_requests_own_merges_alone(
    stack: dict[str, object], tmp_path: Path
) -> None:
    covered = _run(stack, tmp_path, base_ref="feature")

    assert covered == [stack["child_merge"]], covered


def test_the_default_branch_as_base_drags_in_the_parents_merges(
    stack: dict[str, object], tmp_path: Path
) -> None:
    # The control, and the proof the assertion above is not vacuous: the same
    # tree scoped to the default branch reports the parent's merge too, which is
    # the read the stacked case must not buy.
    covered = _run(stack, tmp_path, base_ref="main")

    assert stack["parent_merge"] in covered, covered
