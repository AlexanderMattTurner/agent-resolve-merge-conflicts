"""merged-syntax-check.py, the post-merge check a caller that names none gets.

Driven as the post-merge check runs it: a subprocess whose cwd is a repository
with the merge commit checked out.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT, commit_files, git_out, init_test_repo

SCRIPT = REPO_ROOT / ".github" / "resolver" / "auto-resolve" / "merged-syntax-check.py"


def _merge_base_into_pr(repo: Path, merged_text: str) -> None:
    """Merge `main`, which added c.py, into `pr`, recording MERGED_TEXT as c.py."""
    commit_files(repo, {"a.py": "x = 1\n"}, "root")
    git_out(repo, "checkout", "-q", "-b", "pr")
    commit_files(repo, {"b.py": "y = 2\n"}, "pr side")
    git_out(repo, "checkout", "-q", "main")
    commit_files(repo, {"c.py": "z = 3\n"}, "base side")
    git_out(repo, "checkout", "-q", "pr")
    git_out(repo, "merge", "-q", "--no-ff", "--no-commit", "main")
    (repo / "c.py").write_text(merged_text, encoding="utf-8")
    git_out(repo, "add", "c.py")
    git_out(repo, "commit", "-q", "-m", "merge main")


def _run(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("merged_text", "status", "report"),
    [
        pytest.param(
            "def broken(:\n",
            1,
            "c.py: the merged file does not parse\n",
            id="unparsable",
        ),
        pytest.param("z = 4\n", 0, "", id="parses"),
    ],
)
def test_the_merge_commit_files_are_parsed(
    tmp_path: Path, merged_text: str, status: int, report: str
) -> None:
    init_test_repo(tmp_path)
    _merge_base_into_pr(tmp_path, merged_text)
    done = _run(tmp_path)
    assert (done.returncode, done.stdout) == (status, report), done.stderr


def test_a_checkout_that_is_not_a_merge_has_nothing_to_judge(tmp_path: Path) -> None:
    init_test_repo(tmp_path)
    commit_files(tmp_path, {"a.py": "def broken(:\n"}, "a broken file, no merge")
    done = _run(tmp_path)
    assert (done.returncode, done.stdout) == (0, ""), done.stderr
