"""Shared pytest fixtures for shell-script tests.

`.github/scripts` and `.github/resolver` join `sys.path` here so a test can
import the libraries the lints there import — `_gha_expression` as a sibling,
`repolint._linecheck` as a package — under the same names the lints use.
"""

import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterator

import pytest

from tests._helpers import REPO_ROOT, copy_script_to, git_env, init_test_repo

sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / ".github" / "resolver"))


@pytest.fixture
def empty_git_repo(tmp_path: Path) -> Iterator[Path]:
    """Throwaway git repo with an initial empty commit (so HEAD exists)."""
    init_test_repo(tmp_path)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=tmp_path,
        env=git_env(),
        check=True,
    )
    yield tmp_path


@pytest.fixture
def copy_script() -> Callable[[str, Path], Path]:
    """Return a helper that copies a repo script into a sandbox dir."""
    return copy_script_to
