"""Tests for the missed-rename PORT: staging a relocation's destination with the
three blobs git's own rename detection would have given it, then merging them.

Each case builds a real scratch repo, drives an actual merge through git, and
calls the module in-process against that mid-merge tree.
"""

import subprocess
from pathlib import Path

import pytest

from tests._helpers import commit_files, git_env, git_out, init_test_repo
from tests._resolver_helpers import load_script

relocation = load_script(".github/resolver/auto-resolve/_relocation.py")
port = load_script(".github/resolver/auto-resolve/_relocation_port.py")

_OLD = "sbx/lib/egress_filter.py"
_NEW = "pkg/src/gw/egress_filter.py"
_LAUNCHER = '"""Launcher."""\n\nfrom gw.egress_filter import main\n'


def _body(tail: str = "# tail\n") -> str:
    lines = [
        f"def handler_{n}(request, policy, upstream):  # long enough to sample"
        for n in range(60)
    ]
    lines += [
        f"    return rule_on({n}, policy, upstream)  # another distinctive line"
        for n in range(60)
    ]
    return "\n".join(lines) + "\n" + tail


def _repo(tmp_path: Path, *, mover_is_head: bool, mover_tail: str) -> Path:
    """A mid-merge repo where one side moved the body and the other edited the
    old path's tail. `mover_is_head` flips which side git calls ours."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_OLD: _body()}, "add the filter")
    git_out(repo, "checkout", "-q", "-b", "other")
    commit_files(repo, {_OLD: _body("# STRANDED EDIT\n")}, "edit the old path")
    git_out(repo, "checkout", "-q", "main")
    commit_files(
        repo, {_OLD: _LAUNCHER, _NEW: _body(mover_tail)}, "move into a package"
    )
    if not mover_is_head:
        git_out(repo, "checkout", "-q", "other")
    subprocess.run(
        ["git", "merge", "--no-commit", "main" if not mover_is_head else "other"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        check=False,
    )
    return repo


def _port_one(repo: Path):
    facts = relocation._merge_facts([_OLD])  # noqa: SLF001
    moved = relocation.relocation_for(_OLD, facts)
    assert moved is not None, "the fixture must be a detectable relocation"
    return moved, port.apply_port(moved, repo)


@pytest.mark.parametrize("mover_is_head", [True, False])
def test_the_stranded_edit_lands_on_the_destination(
    tmp_path, monkeypatch, mover_is_head
):
    """The whole point: the other side's edit to the OLD path reaches the body at
    its NEW path, which is what git would have done had it seen the rename. Both
    orientations, because ours/theirs decides which blob is which stage."""
    repo = _repo(tmp_path, mover_is_head=mover_is_head, mover_tail="# tail\n")
    monkeypatch.chdir(repo)

    _moved, ported = _port_one(repo)

    assert ported.merged_clean
    assert "STRANDED EDIT" in (repo / _NEW).read_text(encoding="utf-8")
    assert (repo / _OLD).read_text(encoding="utf-8") == _LAUNCHER
    assert git_out(repo, "diff", "--name-only", "--diff-filter=U") == ""


@pytest.mark.parametrize("mover_is_head", [True, False])
def test_a_real_disagreement_leaves_the_destination_unmerged(
    tmp_path, monkeypatch, mover_is_head
):
    """When both sides changed the same line, the port must NOT invent an answer:
    it leaves the destination genuinely unmerged, so it joins the conflicted set
    and every existing guard applies to it unchanged."""
    repo = _repo(
        tmp_path, mover_is_head=mover_is_head, mover_tail="# MOVER CHANGED THIS\n"
    )
    monkeypatch.chdir(repo)

    _moved, ported = _port_one(repo)

    assert not ported.merged_clean
    assert git_out(repo, "diff", "--name-only", "--diff-filter=U") == _NEW
    assert "<<<<<<<" in (repo / _NEW).read_text(encoding="utf-8")
    stages = git_out(repo, "ls-files", "-u", "--", _NEW).split("\n")
    assert len(stages) == 3, "the destination carries all three merge stages"


def test_the_old_path_is_resolved_to_the_launcher(tmp_path, monkeypatch):
    """Even when the destination conflicts, the old path is settled: its body
    moved, so the launcher is the only content it can correctly hold."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# MOVER CHANGED THIS\n")
    monkeypatch.chdir(repo)

    _port_one(repo)

    assert (repo / _OLD).read_text(encoding="utf-8") == _LAUNCHER
    assert _OLD not in git_out(repo, "diff", "--name-only", "--diff-filter=U")


def test_a_refusal_leaves_the_merge_exactly_as_git_wrote_it(tmp_path, monkeypatch):
    """A port that half-applied is worse than one that never ran, so a missing
    blob refuses with the index untouched."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    monkeypatch.chdir(repo)
    before = git_out(repo, "ls-files", "-s", "-u")
    moved = relocation.Relocation(
        _OLD, "pkg/src/gw/not_here.py", "this PR", "the base branch"
    )

    with pytest.raises(port.PortRefused):
        port.apply_port(moved, repo)

    assert git_out(repo, "ls-files", "-s", "-u") == before


def test_port_relocations_drives_the_whole_unmerged_set(tmp_path, monkeypatch):
    """The entry point prepare.sh and the replay both call: it reads the unmerged
    set itself, so the two derive the same answer with nothing passed between."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    monkeypatch.chdir(repo)

    done = port.port_relocations(repo, set())

    assert [(p.old_path, p.destination, p.merged_clean) for p in done] == [
        (_OLD, _NEW, True)
    ]
    assert git_out(repo, "diff", "--name-only", "--diff-filter=U") == ""
    # Both paths must be STAGED, not merely written: prepare.sh restores every
    # unstaged worktree change before reading the conflict list, so a port left
    # in the worktree alone would be checked out again and silently undone.
    assert git_out(repo, "diff", "--name-only") == ""


def test_a_skipped_path_is_never_ported(tmp_path, monkeypatch):
    """The caller resolves some paths another way; those must keep their conflict."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    monkeypatch.chdir(repo)

    assert port.port_relocations(repo, {_OLD}) == []
    assert git_out(repo, "diff", "--name-only", "--diff-filter=U") == _OLD
