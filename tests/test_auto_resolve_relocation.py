"""Tests for the RELOCATION detector and the notice it feeds a shard.

The shape: one side moves a file's body to a new path and leaves a launcher at
the old one, so git rename detection cannot fire and the whole file conflicts.
Each case builds a real scratch repo, drives the merge through actual git, and
calls the module in-process against that tree.
"""

import subprocess
import sys
from pathlib import Path

from tests._helpers import commit_files, git_env, git_out, init_test_repo
from tests._resolver_helpers import load_script

relocation = load_script(".github/resolver/auto-resolve/_relocation.py")
sys.path.insert(0, str(Path.cwd() / ".github/resolver/auto-resolve"))
prompts = load_script(".github/resolver/auto-resolve/prompts.py")

_OLD_PATH = "sbx-kit/image/lib/egress_filter.py"
_NEW_PATH = "pkg/src/egress_gateway/egress_filter.py"
_LAUNCHER = (
    '"""Start the egress gateway from the package beside this file."""\n'
    "\nfrom egress_gateway.egress_filter import main\n"
    '\nif __name__ == "__main__":\n    main()\n'
)
_SHARED_HEADER = [
    "# Copyright the glovebox authors. All rights reserved.",
    "from egress_gateway.policy_document import configured_policy",
    "from egress_gateway.host_match import granting_entries",
]


def _body(marker: str) -> str:
    """A file long and distinctive enough to be recognised at a new path."""
    lines = list(_SHARED_HEADER)
    lines += [f"def handler_number_{n}(request, policy, upstream):" for n in range(60)]
    lines += [
        f"    return rule_on_the_request({n}, policy, upstream)" for n in range(60)
    ]
    lines.append(f"# {marker}")
    return "\n".join(lines) + "\n"


def _merge(repo: Path, ref: str) -> None:
    subprocess.run(
        ["git", "merge", "--no-commit", ref],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        check=False,
    )


def _facts(paths: list[str]):
    return relocation._merge_facts(paths)  # noqa: SLF001


def _repo_with_the_move_on(tmp_path: Path, mover: str) -> Path:
    """A repo mid-merge where MOVER ("checked-out" or "merged-in") is the side
    that moved the body away and left a launcher behind."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_OLD_PATH: _body("base")}, "add the filter")
    git_out(repo, "checkout", "-q", "-b", "other")
    commit_files(repo, {_OLD_PATH: _body("edited on the other side")}, "edit it")
    git_out(repo, "checkout", "-q", "main")
    commit_files(
        repo, {_OLD_PATH: _LAUNCHER, _NEW_PATH: _body("base")}, "move into a package"
    )
    if mover == "merged-in":
        git_out(repo, "checkout", "-q", "other")
        _merge(repo, "main")
    else:
        _merge(repo, "other")
    return repo


def test_names_the_path_the_body_moved_to(tmp_path, monkeypatch):
    """The shape agent-glovebox #5289 hit: a launcher at the old path defeats
    rename detection, so the detector has to find the new home itself."""
    repo = _repo_with_the_move_on(tmp_path, "checked-out")
    monkeypatch.chdir(repo)
    assert _OLD_PATH in git_out(repo, "diff", "--name-only", "--diff-filter=U")

    found = relocation.relocation_for(_OLD_PATH, _facts([_OLD_PATH]))

    assert found is not None
    assert found.destination == _NEW_PATH
    assert found.stub_side == "this PR"
    assert found.stranded_side == "the base branch"


def test_the_mover_is_named_whichever_side_it_is(tmp_path, monkeypatch):
    """The same move arriving from the OTHER side: the two labels must swap, or
    the notice tells the wrong side to port its work."""
    repo = _repo_with_the_move_on(tmp_path, "merged-in")
    monkeypatch.chdir(repo)

    found = relocation.relocation_for(_OLD_PATH, _facts([_OLD_PATH]))

    assert found is not None
    assert found.destination == _NEW_PATH
    assert found.stub_side == "the base branch"
    assert found.stranded_side == "this PR"


def test_an_ordinary_conflict_is_not_a_relocation(tmp_path, monkeypatch):
    """Both sides editing one file must not be read as a move: a false positive
    tells the shard one side is redundant."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_OLD_PATH: _body("base")}, "add the filter")
    git_out(repo, "checkout", "-q", "-b", "other")
    commit_files(repo, {_OLD_PATH: _body("edited on the other side")}, "other edit")
    git_out(repo, "checkout", "-q", "main")
    commit_files(repo, {_OLD_PATH: _body("edited on main")}, "main edit")
    _merge(repo, "other")
    monkeypatch.chdir(repo)

    assert relocation.relocation_for(_OLD_PATH, _facts([_OLD_PATH])) is None


def test_an_unrelated_new_file_of_the_same_name_is_not_the_destination(
    tmp_path, monkeypatch
):
    """The basename narrows the search; the CONTENT decides. A branch that guts
    the old file and adds an unrelated namesake must not capture the decline."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_OLD_PATH: _body("base")}, "add the filter")
    git_out(repo, "checkout", "-q", "-b", "other")
    commit_files(repo, {_OLD_PATH: _body("edited on the other side")}, "other edit")
    git_out(repo, "checkout", "-q", "main")
    commit_files(
        repo,
        {_OLD_PATH: _LAUNCHER, _NEW_PATH: "# a different file that shares a name\n"},
        "gut the filter, add a namesake",
    )
    _merge(repo, "other")
    monkeypatch.chdir(repo)

    assert relocation.relocation_for(_OLD_PATH, _facts([_OLD_PATH])) is None


def _long_body(marker: str, count: int) -> str:
    """A body far longer than the sample cap, so WHERE the sample is taken from
    decides the verdict."""
    lines = [
        f"def handler_number_{n}(request, policy, upstream): pass" for n in range(count)
    ]
    lines.append(f"# {marker}")
    return "\n".join(lines) + "\n"


def test_a_candidate_holding_only_the_files_first_part_is_not_the_destination(
    tmp_path, monkeypatch
):
    """A file SPLIT in two: one new file takes the first chunk, the rest goes
    elsewhere. Sampling the base's first lines alone would see every one of them
    in that chunk and call it the destination, so the sample is spread across the
    whole body instead. 130 of 400 handlers is well under the match bar."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_OLD_PATH: _long_body("base", 400)}, "add the filter")
    git_out(repo, "checkout", "-q", "-b", "other")
    commit_files(repo, {_OLD_PATH: _long_body("edited elsewhere", 400)}, "other edit")
    git_out(repo, "checkout", "-q", "main")
    first_chunk = _long_body("base", 130)
    commit_files(
        repo, {_OLD_PATH: _LAUNCHER, _NEW_PATH: first_chunk}, "split off the first part"
    )
    _merge(repo, "other")
    monkeypatch.chdir(repo)

    assert relocation.relocation_for(_OLD_PATH, _facts([_OLD_PATH])) is None


def test_the_launcher_is_what_creates_the_conflict(tmp_path, monkeypatch):
    """The same move with NO launcher left behind: git detects the rename,
    carries the other side's edit onto the new path, and nothing conflicts. The
    contrast that says the launcher, not the move, is the cause."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_OLD_PATH: _body("base")}, "add the filter")
    git_out(repo, "checkout", "-q", "-b", "other")
    commit_files(repo, {_OLD_PATH: _body("edited on the other side")}, "other edit")
    git_out(repo, "checkout", "-q", "main")
    (repo / _OLD_PATH).unlink()
    commit_files(repo, {_NEW_PATH: _body("base")}, "move with no launcher")
    _merge(repo, "other")
    monkeypatch.chdir(repo)

    assert git_out(repo, "diff", "--name-only", "--diff-filter=U") == ""
    assert "edited on the other side" in (repo / _NEW_PATH).read_text(encoding="utf-8")


def test_a_path_it_cannot_read_drops_that_path_and_not_the_run(tmp_path, monkeypatch):
    """A conflicted path whose bytes are not UTF-8 must not end the fan-out:
    run_git decodes strictly, and relocations() only enriches a prompt."""
    repo = tmp_path / "repo"
    binary = "assets/logo.bin"
    init_test_repo(repo)
    commit_files(repo, {_OLD_PATH: _body("base"), binary: "base\n"}, "add both")
    git_out(repo, "checkout", "-q", "-b", "other")
    commit_files(repo, {_OLD_PATH: _body("edited on the other side")}, "other edit")
    (repo / binary).write_bytes(b"\xff\xfe\x00other\xff")
    git_out(repo, "add", binary)
    commit_files(repo, {}, "other binary edit")
    git_out(repo, "checkout", "-q", "main")
    commit_files(
        repo, {_OLD_PATH: _LAUNCHER, _NEW_PATH: _body("base")}, "move into a package"
    )
    (repo / binary).write_bytes(b"\xfe\xff\x00main\xfe")
    git_out(repo, "add", binary)
    commit_files(repo, {}, "main binary edit")
    _merge(repo, "other")
    monkeypatch.chdir(repo)
    conflicted = git_out(repo, "diff", "--name-only", "--diff-filter=U").split("\n")
    assert binary in conflicted and _OLD_PATH in conflicted

    found = relocation.relocations(conflicted, set())

    assert binary not in found
    assert found[_OLD_PATH].destination == _NEW_PATH


def test_skipped_paths_are_never_judged(tmp_path, monkeypatch):
    """A modify/delete or sidecar path must not be forced into a whole-file
    shard by this: neither can act on the notice."""
    repo = _repo_with_the_move_on(tmp_path, "checked-out")
    monkeypatch.chdir(repo)

    assert relocation.relocations([_OLD_PATH], {_OLD_PATH}) == {}


def test_the_notice_tells_the_shard_to_keep_the_markers():
    """The decline is published by the marker sweep, so a notice that removed
    the markers would drop the stranded side silently."""
    moved = relocation.Relocation(_OLD_PATH, _NEW_PATH, "this PR", "the base branch")

    text = prompts.relocation_notice(_OLD_PATH, moved)

    assert _NEW_PATH in text
    assert "LEAVE this file's conflict markers exactly as you found them." in text
    assert "Record a DECLINE" in text
    assert prompts.relocation_notice(_OLD_PATH, None) == ""


def test_the_notice_reaches_the_shard_prompt():
    """A whole-file shard's prompt must carry it; every other shard must not."""
    moved = relocation.Relocation(_OLD_PATH, _NEW_PATH, "this PR", "the base branch")

    carried = prompts.shard_prompt("5289", _OLD_PATH, "/tmp/decline.json", "h", moved)

    assert _NEW_PATH in carried
    assert "RELOCATION" not in prompts.shard_prompt("5289", _OLD_PATH, "/d", "h")
