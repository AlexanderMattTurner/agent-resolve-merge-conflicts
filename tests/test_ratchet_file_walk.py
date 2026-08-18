"""`tracked_like_files` reads the repository, and nothing `.gitignore` excludes."""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts" / "checks"))

from _ratchet import tracked_like_files  # noqa: E402


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "kept.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "second-tree.py").write_text("y = 2\n", encoding="utf-8")
    return tmp_path


def test_an_ignored_directory_is_not_walked(repo: Path):
    # A sub-agent worktree lands under an ignored path and holds a whole second
    # copy of the tree; counting it charges one repository's violations to another.
    assert "ignored/second-tree.py" not in tracked_like_files(repo)


def test_the_repository_s_own_files_are_walked(repo: Path):
    found = tracked_like_files(repo)
    assert "kept.py" in found
    assert ".gitignore" in found


def test_a_scratch_tree_outside_git_still_walks(tmp_path: Path):
    # `git ls-files` fails outside a repository, and the walk must not go empty:
    # an empty scan is a check that reports success having read nothing.
    (tmp_path / "loose.py").write_text("z = 3\n", encoding="utf-8")
    assert tracked_like_files(tmp_path) == ["loose.py"]
