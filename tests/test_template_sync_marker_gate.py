"""No file on the `template-sync` branch may carry conflict markers.

template-sync.sh writes diff3 markers into the tree deliberately, and
create-pull-request commits that tree before either resolver tier runs. So every
file a tier fails to settle reaches the branch with `<<<<<<< local` still in it,
and until this gate nothing failed: the resolve driver printed a `::warning::`
for its `unresolved` set and exited 0. Downstream that shipped a bash library
whose markers are a syntax error (agent-sanitizer#305,
agent-control-plane-core#52).

Each case drives the real script against a real git remote and asserts on the
branch the remote ends up holding, because the branch is what a consumer checks
out.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT, commit_files, git_env, git_out, init_test_repo

SCRIPT = REPO_ROOT / ".github" / "scripts" / "template-sync-marker-gate.sh"

MARKED = (
    "retry_with_backoff() {\n"
    "<<<<<<< local\n"
    "  local tries=5\n"
    "||||||| base\n"
    "  local tries=2\n"
    "=======\n"
    "  local tries=3\n"
    ">>>>>>> template\n"
    "}\n"
)
LOCAL = "retry_with_backoff() {\n  local tries=5\n}\n"


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path, str]:
    """A work repo with a bare `origin`, `main` holding the pre-sync copies, and
    `template-sync` checked out. Returns (work, origin, base_sha)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main"], cwd=origin, check=True
    )

    work = tmp_path / "work"
    init_test_repo(work)
    base_sha = commit_files(
        work,
        {".github/scripts/lib/retry.bash": LOCAL, "README.md": "# repo\n"},
        "pre-sync",
    )
    env = git_env()
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)], cwd=work, check=True
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=work, env=env, check=True
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "template-sync"], cwd=work, check=True
    )
    return work, origin, base_sha


def run_gate(
    work: Path,
    base_sha: str,
    conflict_files: tuple[str, ...] = (),
    extra_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = git_env()
    if extra_path is not None:
        env["PATH"] = f"{extra_path}{os.pathsep}{env.get('PATH', os.environ['PATH'])}"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=work,
        env={
            **env,
            "BASE_SHA": base_sha,
            "CONFLICT_FILES": " ".join(conflict_files),
            "GITHUB_TOKEN": "x",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def pushed(work: Path, path: str) -> str:
    """The content the remote's `template-sync` tip holds for PATH."""
    subprocess.run(
        ["git", "fetch", "-q", "origin"], cwd=work, env=git_env(), check=True
    )
    return git_out(work, "show", f"origin/template-sync:{path}")


def test_a_marked_file_is_withheld_and_the_run_goes_red(sandbox):
    work, _, base_sha = sandbox
    commit_files(
        work,
        {".github/scripts/lib/retry.bash": MARKED, "README.md": "# repo v2\n"},
        "sync from template",
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "template-sync"],
        cwd=work,
        env=git_env(),
        check=True,
    )

    result = run_gate(work, base_sha, (".github/scripts/lib/retry.bash",))

    assert result.returncode == 1, result.stdout + result.stderr
    assert ".github/scripts/lib/retry.bash" in result.stdout
    # The conflicted file goes back to what this repo had; every other synced
    # change on the branch survives, so the gate withholds one file, not the sync.
    assert pushed(work, ".github/scripts/lib/retry.bash") == LOCAL.rstrip("\n")
    assert pushed(work, "README.md") == "# repo v2"


def test_a_file_the_base_does_not_have_is_removed(sandbox):
    """A tier can only create a marked path that the pre-sync repo never had.
    There is no earlier content to restore, so the branch must lose the file."""
    work, _, base_sha = sandbox
    commit_files(work, {"new-from-template.bash": MARKED}, "sync from template")
    subprocess.run(
        ["git", "push", "-q", "origin", "template-sync"],
        cwd=work,
        env=git_env(),
        check=True,
    )

    result = run_gate(work, base_sha, ("new-from-template.bash",))

    assert result.returncode == 1, result.stdout + result.stderr
    tracked = git_out(work, "ls-tree", "-r", "--name-only", "origin/template-sync")
    assert "new-from-template.bash" not in tracked.splitlines()


def test_an_uncommitted_marked_edit_is_not_the_branch(sandbox):
    """The resolve step leaves edits it chose not to push. A consumer fetches the
    branch, so the workspace must not decide the verdict."""
    work, _, base_sha = sandbox
    tip = commit_files(work, {"README.md": "# repo v2\n"}, "sync from template")
    subprocess.run(
        ["git", "push", "-q", "origin", "template-sync"],
        cwd=work,
        env=git_env(),
        check=True,
    )
    (work / ".github" / "scripts" / "lib" / "retry.bash").write_text(
        MARKED, encoding="utf-8"
    )

    result = run_gate(work, base_sha)

    assert result.returncode == 0, result.stdout + result.stderr
    subprocess.run(
        ["git", "fetch", "-q", "origin"], cwd=work, env=git_env(), check=True
    )
    assert git_out(work, "rev-parse", "origin/template-sync") == tip


def test_a_clean_branch_passes_and_is_left_alone(sandbox):
    work, _, base_sha = sandbox
    tip = commit_files(
        work,
        {".github/scripts/lib/retry.bash": "  local tries=4\n"},
        "sync from template",
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "template-sync"],
        cwd=work,
        env=git_env(),
        check=True,
    )

    result = run_gate(work, base_sha)

    assert result.returncode == 0, result.stdout + result.stderr
    subprocess.run(
        ["git", "fetch", "-q", "origin"], cwd=work, env=git_env(), check=True
    )
    assert git_out(work, "rev-parse", "origin/template-sync") == tip


def test_a_new_file_with_marker_text_outside_the_conflict_set_is_left_alone(sandbox):
    """committed_marker_paths greps the WHOLE tree, so it would also name a file
    the template shipped legitimately (a new fixture, no base copy, no merge
    conflict) if that file merely contains marker-shaped text. CONFLICT_FILES —
    the sync's own record of which paths its merge actually conflicted on — is
    what keeps the gate's destructive git-rm branch off a file that was never
    one of those."""
    work, _, base_sha = sandbox
    tip = commit_files(work, {"docs/example-conflict.md": MARKED}, "sync from template")
    subprocess.run(
        ["git", "push", "-q", "origin", "template-sync"],
        cwd=work,
        env=git_env(),
        check=True,
    )

    result = run_gate(work, base_sha)  # empty conflict_files: nothing conflicted

    assert result.returncode == 0, result.stdout + result.stderr
    subprocess.run(
        ["git", "fetch", "-q", "origin"], cwd=work, env=git_env(), check=True
    )
    assert git_out(work, "rev-parse", "origin/template-sync") == tip


def test_a_path_whose_base_copy_is_also_marked_is_left_alone(sandbox):
    """committed_marker_paths drops a path whose every current marker block
    already existed in the base copy, so a repository that keeps marker text
    as a fixture is never withheld from itself."""
    work, _, _ = sandbox
    tip = commit_files(work, {"tests/marker_fixture.txt": MARKED}, "marker fixture")
    subprocess.run(
        ["git", "push", "-q", "origin", "template-sync"],
        cwd=work,
        env=git_env(),
        check=True,
    )

    result = run_gate(work, tip, ("tests/marker_fixture.txt",))

    assert result.returncode == 0, result.stdout + result.stderr
    subprocess.run(
        ["git", "fetch", "-q", "origin"], cwd=work, env=git_env(), check=True
    )
    assert git_out(work, "rev-parse", "origin/template-sync") == tip


def push_branch(work: Path) -> None:
    subprocess.run(
        ["git", "push", "-q", "origin", "template-sync"],
        cwd=work,
        env=git_env(),
        check=True,
    )


def test_a_base_the_checkout_cannot_resolve_stops_the_gate(sandbox):
    """BASE_SHA says what each marked file goes back to, and a path it cannot
    resolve is deleted. An unreadable base must stop the gate, not empty it."""
    work, _, _ = sandbox
    commit_files(work, {".github/scripts/lib/retry.bash": MARKED}, "sync from template")
    push_branch(work)

    result = run_gate(work, "0" * 40, (".github/scripts/lib/retry.bash",))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "is not a commit" in result.stdout
    assert pushed(work, ".github/scripts/lib/retry.bash") == MARKED.rstrip("\n")


def test_a_failed_scan_never_reads_as_a_clean_branch(sandbox, tmp_path):
    """The scan is the gate's only evidence. A `git grep` that dies must reach
    the caller as a failure, not as an empty list that looks like no markers."""
    work, _, base_sha = sandbox
    commit_files(work, {".github/scripts/lib/retry.bash": MARKED}, "sync from template")
    push_branch(work)
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    real_git = shutil.which("git")
    assert real_git, "the stub forwards to the real git; without it this proves nothing"
    (stub_dir / "git").write_text(
        # The scan calls `git -C <repo> grep`, so match the subcommand anywhere.
        f'#!/usr/bin/env bash\nfor a in "$@"; do [[ "$a" == grep ]] && exit 128; done\n'
        f'exec {real_git} "$@"\n',
        encoding="utf-8",
    )
    (stub_dir / "git").chmod(0o755)

    result = run_gate(
        work, base_sha, (".github/scripts/lib/retry.bash",), extra_path=stub_dir
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "the marker scan failed" in result.stdout
    assert "no conflict markers" not in result.stdout
