"""Carrying one run's partial resolution into the next run's merge.

Every case drives the real script against a real mid-merge git repository: the
question it answers is what the INDEX and the WORKTREE hold afterwards, which no
stub can stand in for.
"""

# covers: .github/resolver/auto-resolve/apply-salvage.py

import json
import subprocess
from pathlib import Path

import pytest

from tests._resolver_helpers import REPO_ROOT, load_script

SCRIPT = REPO_ROOT / ".github" / "resolver" / "auto-resolve" / "apply-salvage.py"

# The shared runner this script reaches git through. Driven here in-process
# because the cases that matter are the ones the script REPORTS on: a non-zero
# status with its stderr, and a command that reads stdin.
git_io = load_script(".github/resolver/auto-resolve/_git_io.py")

CARRIED = "carried.txt"
STILL_CONFLICTED = "left.txt"


def git(cwd: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=check,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    )
    return done.stdout


def mid_merge_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository left mid-merge on two conflicted paths.

    Returns the worktree, the merge base, and the head SHA — the two values the
    salvage manifest pins itself to."""
    work = tmp_path / "repo"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    for name in (CARRIED, STILL_CONFLICTED):
        (work / name).write_text("common\n", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-qm", "seed")
    merge_base = git(work, "rev-parse", "HEAD").strip()
    git(work, "checkout", "-qb", "side")
    for name in (CARRIED, STILL_CONFLICTED):
        (work / name).write_text("base rewrite\n", encoding="utf-8")
    git(work, "commit", "-qam", "the base branch reworks both paths")
    git(work, "checkout", "-q", "main")
    for name in (CARRIED, STILL_CONFLICTED):
        (work / name).write_text("pr rewrite\n", encoding="utf-8")
    git(work, "commit", "-qam", "the PR reworks both paths")
    head = git(work, "rev-parse", "HEAD").strip()
    git(work, "merge", "--no-commit", "side", check=False)
    return work, merge_base, head


def salvage(
    tmp_path: Path, work: Path, merge_base: str, head: str, pins: dict | None = None
) -> Path:
    """A salvage directory holding a resolution of CARRIED, as a refusing run
    writes it: the patch is `git diff <merge base> -- <paths>` over the resolved
    content, and the manifest pins the head and that base."""
    resolved = tmp_path / "salvage"
    resolved.mkdir(exist_ok=True)
    original = (work / CARRIED).read_text(encoding="utf-8")
    (work / CARRIED).write_text("resolved by the last round\n", encoding="utf-8")
    patch = git(work, "diff", merge_base, "--", CARRIED)
    (work / CARRIED).write_text(original, encoding="utf-8")
    (resolved / "salvage.patch").write_text(patch, encoding="utf-8")
    document = {
        "head": head,
        "merge_base": merge_base,
        "paths": [CARRIED],
        "round": 1,
        **(pins or {}),
    }
    (resolved / "salvage.json").write_text(json.dumps(document), encoding="utf-8")
    return resolved


def run(work: Path, salvage_dir: Path | str, head: str, merge_base: str):
    return subprocess.run(
        ["python3", str(SCRIPT)],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(work),
            "SALVAGE_DIR": str(salvage_dir),
            "HEAD_SHA": head,
            "MERGE_BASE": merge_base,
        },
    )


def unmerged(work: Path) -> set[str]:
    """The paths git still reports as conflicted."""
    listed = git(work, "diff", "--name-only", "--diff-filter=U")
    return {line for line in listed.splitlines() if line}


def test_git_result_carries_back_the_status_and_the_stderr(tmp_path, monkeypatch):
    """`git` exits on a failure and `git_status` throws both streams away, so
    neither can say WHY a carry did not apply."""
    work, _, _ = mid_merge_repo(tmp_path)
    monkeypatch.setattr(git_io, "_REPO", None)
    git_io.bind_repo(work)
    failed = git_io.git_result("checkout", "0" * 40, "--", CARRIED)
    assert failed.returncode != 0
    assert failed.stderr.strip()


def test_git_result_feeds_stdin_to_the_plumbing_that_reads_it(tmp_path, monkeypatch):
    """`update-index --index-info` takes the stages on stdin, which is how the
    carry's rollback puts a conflict back."""
    work, _, _ = mid_merge_repo(tmp_path)
    monkeypatch.setattr(git_io, "_REPO", None)
    git_io.bind_repo(work)
    stages = git_io.git_result("ls-files", "-u", "--", CARRIED).stdout
    git_io.git_result("checkout", "--ours", "--", CARRIED)
    git_io.git_result("add", "--", CARRIED)
    assert CARRIED not in unmerged(work)
    restored = git_io.git_result("update-index", "--index-info", stdin=stages)
    assert restored.returncode == 0, restored.stderr
    assert CARRIED in unmerged(work)


def test_a_carried_path_is_staged_and_leaves_the_conflict_set(tmp_path):
    """The whole point: the next run's conflict list is what the last run did
    not finish, so its window buys only the remainder."""
    work, merge_base, head = mid_merge_repo(tmp_path)
    assert unmerged(work) == {CARRIED, STILL_CONFLICTED}
    result = run(work, salvage(tmp_path, work, merge_base, head), head, merge_base)
    assert result.returncode == 0, result.stderr
    assert unmerged(work) == {STILL_CONFLICTED}
    assert (work / CARRIED).read_text(encoding="utf-8") == (
        "resolved by the last round\n"
    )
    # Staged, not merely written: bundle reads the index, and an unstaged carry
    # would read as an edit the model made outside its assignment.
    assert CARRIED in git(work, "diff", "--cached", "--name-only")
    assert "carried 1 path(s)" in result.stdout


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"head": "0" * 40}, "and this run is on"),
        ({"merge_base": "0" * 40}, "it was cut from merge base"),
        ({"paths": []}, "names no usable paths"),
        ({"paths": [""]}, "names no usable paths"),
    ],
    ids=["another_head", "another_merge_base", "no_paths", "empty_path"],
)
def test_a_salvage_that_does_not_pin_to_this_merge_carries_nothing(
    tmp_path, overrides, reason
):
    """Both pins refuse rather than guess. A patch cut against another head or
    another merge base applies to text neither side of THIS merge wrote."""
    work, merge_base, head = mid_merge_repo(tmp_path)
    result = run(
        work,
        salvage(tmp_path, work, merge_base, head, overrides),
        head,
        merge_base,
    )
    assert result.returncode == 0, result.stderr
    assert reason in result.stdout
    assert unmerged(work) == {CARRIED, STILL_CONFLICTED}


def test_a_patch_that_does_not_apply_puts_the_whole_conflict_back(tmp_path):
    """The refusal that has to be exact: a failed carry must leave the merge as
    git wrote it, stages and all, or the run resolves a tree nobody made."""
    work, merge_base, head = mid_merge_repo(tmp_path)
    salvage_dir = salvage(tmp_path, work, merge_base, head)
    (salvage_dir / "salvage.patch").write_text(
        "diff --git a/carried.txt b/carried.txt\n"
        "--- a/carried.txt\n"
        "+++ b/carried.txt\n"
        "@@ -1 +1 @@\n"
        "-text no side of this merge ever held\n"
        "+something else\n",
        encoding="utf-8",
    )
    before = git(work, "ls-files", "-u")
    conflicted_text = (work / CARRIED).read_text(encoding="utf-8")

    result = run(work, salvage_dir, head, merge_base)

    assert result.returncode == 0, result.stderr
    assert "did not apply to this merge" in result.stdout
    assert unmerged(work) == {CARRIED, STILL_CONFLICTED}
    assert git(work, "ls-files", "-u") == before
    assert (work / CARRIED).read_text(encoding="utf-8") == conflicted_text


def test_an_unreadable_manifest_carries_nothing(tmp_path):
    """A truncated download, a half-written file: the run resolves the whole set
    the way it did before this step existed."""
    work, merge_base, head = mid_merge_repo(tmp_path)
    salvage_dir = salvage(tmp_path, work, merge_base, head)
    (salvage_dir / "salvage.json").write_text("{not json", encoding="utf-8")
    result = run(work, salvage_dir, head, merge_base)
    assert result.returncode == 0, result.stderr
    assert "manifest could not be read" in result.stdout
    assert unmerged(work) == {CARRIED, STILL_CONFLICTED}


def test_no_salvage_directory_is_an_ordinary_run(tmp_path):
    """The common case — a first round carries nothing and says nothing."""
    work, merge_base, head = mid_merge_repo(tmp_path)
    result = run(work, "", head, merge_base)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert unmerged(work) == {CARRIED, STILL_CONFLICTED}
