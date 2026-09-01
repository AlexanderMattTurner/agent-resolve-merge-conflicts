"""Tests for the RELOCATION detector (regression for agent-glovebox #5289: a
branch cut `sbx-kit/image/lib/egress_filter.py` from 1523 lines to a 14-line
launcher while the base branch kept editing the old path, and the shard left
the markers in). Each case builds a real scratch repo and drives the merge
through actual git, then calls the detector in-process against that tree."""

import subprocess
from pathlib import Path

from tests._helpers import commit_files, git_env, git_out, init_test_repo
from tests._resolver_helpers import load_script

relocation = load_script(".github/resolver/auto-resolve/_relocation.py")

_OLD_PATH = "sbx-kit/image/lib/egress_filter.py"
_NEW_PATH = "pkg/src/egress_gateway/egress_filter.py"
_LAUNCHER = (
    '"""Start the egress gateway from the package baked beside this file."""\n'
    "\nfrom egress_gateway.egress_filter import main\n"
    '\nif __name__ == "__main__":\n    main()\n'
)


def _body(marker: str) -> str:
    """A file long and distinctive enough to be recognised at a new path."""
    lines = [f"def handler_number_{n}(request, policy, upstream):" for n in range(60)]
    lines += [
        f"    return rule_on_the_request({n}, policy, upstream)" for n in range(60)
    ]
    lines.append(f"# {marker}")
    return "\n".join(lines) + "\n"


def _repo_at_conflict(tmp_path: Path, *, leave_stub: bool = True) -> Path:
    """A repo mid-merge, with the branch having moved the body away."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_OLD_PATH: _body("base")}, "add the filter")
    git_out(repo, "checkout", "-q", "-b", "branch")
    moved = {_NEW_PATH: _body("base")}
    if leave_stub:
        moved[_OLD_PATH] = _LAUNCHER
    else:
        (repo / _OLD_PATH).unlink()
    commit_files(repo, moved, "move the filter into a package")
    if not leave_stub:
        git_out(repo, "add", "-A")
        subprocess.run(
            ["git", "commit", "--amend", "--no-edit"],
            cwd=repo,
            env=git_env(),
            check=True,
        )
    git_out(repo, "checkout", "-q", "main")
    commit_files(repo, {_OLD_PATH: _body("edited on main")}, "edit the old path")
    git_out(repo, "checkout", "-q", "branch")
    subprocess.run(
        ["git", "merge", "--no-commit", "main"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        check=False,
    )
    return repo


def test_names_the_path_the_body_moved_to(tmp_path, monkeypatch):
    """The shape #5289 hit: a stub at the old path defeats rename detection, so
    the detector has to find the new home itself."""
    repo = _repo_at_conflict(tmp_path)
    monkeypatch.chdir(repo)
    assert _OLD_PATH in git_out(repo, "diff", "--name-only", "--diff-filter=U")

    found = relocation.relocation_for(_OLD_PATH)

    assert found is not None
    assert found.destination == _NEW_PATH
    assert found.stub_side == "this PR"
    assert found.stranded_side == "the base branch"


def test_an_ordinary_conflict_is_not_a_relocation(tmp_path, monkeypatch):
    """Both sides editing one file must not be read as a move: a false positive
    tells the shard to throw a side away."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_OLD_PATH: _body("base")}, "add the filter")
    git_out(repo, "checkout", "-q", "-b", "branch")
    commit_files(repo, {_OLD_PATH: _body("edited on the branch")}, "branch edit")
    git_out(repo, "checkout", "-q", "main")
    commit_files(repo, {_OLD_PATH: _body("edited on main")}, "main edit")
    git_out(repo, "checkout", "-q", "branch")
    subprocess.run(
        ["git", "merge", "--no-commit", "main"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        check=False,
    )
    monkeypatch.chdir(repo)

    assert relocation.relocation_for(_OLD_PATH) is None


def test_the_stub_is_what_creates_the_conflict(tmp_path, monkeypatch):
    """The same move with NO stub left behind: git detects the rename, carries
    the other side's edit onto the new path, and nothing conflicts at all. This
    is the contrast that says the stub — not the move — is the cause."""
    repo = _repo_at_conflict(tmp_path, leave_stub=False)
    monkeypatch.chdir(repo)

    assert git_out(repo, "diff", "--name-only", "--diff-filter=U") == ""
    assert "edited on main" in (repo / _NEW_PATH).read_text(encoding="utf-8")


def test_an_unrelated_new_file_of_the_same_name_is_not_the_destination(
    tmp_path, monkeypatch
):
    """The basename narrows the search; the CONTENT is what decides. A branch
    that guts the old file and happens to add an unrelated `egress_filter.py`
    must not send the shard's decline to that path."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_OLD_PATH: _body("base")}, "add the filter")
    git_out(repo, "checkout", "-q", "-b", "branch")
    commit_files(
        repo,
        {_OLD_PATH: _LAUNCHER, _NEW_PATH: "# a different file that shares a name\n"},
        "gut the filter, add a namesake",
    )
    git_out(repo, "checkout", "-q", "main")
    commit_files(repo, {_OLD_PATH: _body("edited on main")}, "edit the old path")
    git_out(repo, "checkout", "-q", "branch")
    subprocess.run(
        ["git", "merge", "--no-commit", "main"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        check=False,
    )
    monkeypatch.chdir(repo)

    assert relocation.relocation_for(_OLD_PATH) is None
