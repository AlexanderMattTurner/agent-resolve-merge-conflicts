"""A shard that rewrites a script must not ship it without the mode the merge gives it.

Each case drives a real `git merge --no-commit`, replaces the merged file the way a
shard's whole-file write does, and asks `restore_merged_modes` to settle the index.

# covers: .github/resolver/auto-resolve/_merged_modes.py
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import commit_all, commit_files, git_env, git_out, init_test_repo
from tests._resolver_helpers import load_script

modes = load_script(".github/resolver/auto-resolve/_merged_modes.py")

_SCRIPT = "run.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, env=git_env(), check=True, capture_output=True
    )


def _merged(tmp_path: Path, *, head_drops_x: bool) -> Path:
    """`run.sh` is executable at the base; each side edits a different line, and the
    head side optionally removes the executable bit on purpose."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_SCRIPT: "#!/bin/sh\na\nb\nc\nd\n"}, "base")
    (repo / _SCRIPT).chmod(0o755)
    commit_all(repo, "executable")
    _git(repo, "checkout", "-q", "-b", "feature")
    commit_files(repo, {_SCRIPT: "#!/bin/sh\nA\nb\nc\nd\n"}, "feature")
    if head_drops_x:
        (repo / _SCRIPT).chmod(0o644)
        commit_all(repo, "not a script any more")
    _git(repo, "checkout", "-q", "main")
    commit_files(repo, {_SCRIPT: "#!/bin/sh\na\nb\nc\nD\n"}, "main")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "--no-commit", "--no-ff", "main")
    # The shard's write: a fresh file at the default mode, then staged.
    (repo / _SCRIPT).unlink()
    (repo / _SCRIPT).write_text("#!/bin/sh\nA\nb\nc\nD\n", encoding="utf-8")
    (repo / _SCRIPT).chmod(0o644)
    _git(repo, "add", "--", _SCRIPT)
    # The `_git_io` the module imported by name, not a second copy `load_script` would build.
    sys.modules["_git_io"].bind_repo(repo)
    return repo


def _staged_mode(repo: Path) -> str:
    return git_out(repo, "ls-files", "-s", "--", _SCRIPT).split()[0]


@pytest.mark.parametrize(
    ("head_drops_x", "wanted", "corrected"),
    [(False, "100755", [_SCRIPT]), (True, "100644", [])],
    ids=["dropped_bit_restored", "head_removal_kept"],
)
def test_the_staged_mode_is_the_merged_mode(tmp_path, head_drops_x, wanted, corrected):
    repo = _merged(tmp_path, head_drops_x=head_drops_x)
    # Shas, as bundle.py passes: `tree_entry` caches by revision name.
    ours, theirs = (git_out(repo, "rev-parse", rev) for rev in ("HEAD", "MERGE_HEAD"))
    assert modes.restore_merged_modes([_SCRIPT], ours, theirs) == corrected
    assert _staged_mode(repo) == wanted
    # The worktree agrees, so a later `git add` of the path keeps the answer.
    assert bool((repo / _SCRIPT).stat().st_mode & 0o100) is (wanted == "100755")


@pytest.mark.parametrize(
    ("base", "ours", "theirs", "wanted"),
    [
        ("100644", "100644", "100755", "100755"),
        ("100644", "100755", "100644", "100755"),
        ("100644", "100755", "120000", None),
        (None, "100755", "100644", None),
        ("100755", "100755", "100755", "100755"),
    ],
    ids=["theirs_changed", "ours_changed", "both_changed", "both_added", "unchanged"],
)
def test_merged_mode_settles_the_side_that_changed_it(base, ours, theirs, wanted):
    """The side that changed the mode wins; both sides changing it differently has
    no merged answer, which is what keeps a symlink or submodule mode unwritten."""
    assert modes.merged_mode(base, ours, theirs) == wanted
