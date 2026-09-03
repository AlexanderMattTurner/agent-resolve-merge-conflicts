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


def _moved(path: str, destination: str) -> "relocation.Relocation":
    return relocation.Relocation(
        path=path,
        destination=destination,
        stub_side="this PR",
        stranded_side="the base branch",
        stub_stage=":2",
        stub_ref="HEAD",
        stranded_stage=":3",
        stranded_ref="MERGE_HEAD",
    )


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

    with pytest.raises(port.PortRefused):
        port.apply_port(_moved(_OLD, "pkg/src/gw/not_here.py"), repo)

    assert git_out(repo, "ls-files", "-s", "-u") == before


def test_the_port_runs_inside_a_linked_worktree(tmp_path, monkeypatch):
    """land.sh replays the merge with `git worktree add`, where `.git` is a FILE.
    Every other case here uses a primary checkout, which is how a scratch dir
    under `.git` passed CI while making the replay's port impossible — and the
    replay is the one place it has to work, or the composed tree discards it."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    git_out(repo, "merge", "--abort")
    raw = tmp_path / "replay"
    git_out(repo, "worktree", "add", "--detach", "--quiet", str(raw), "main")
    subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "other"],
        cwd=raw,
        env=git_env(),
        capture_output=True,
        check=False,
    )
    assert (raw / ".git").is_file(), "the fixture must be a LINKED worktree"
    monkeypatch.chdir(raw)

    done = port.port_relocations(raw, set())

    assert [(p.old_path, p.merged_clean) for p in done] == [(_OLD, True)]
    assert "STRANDED EDIT" in (raw / _NEW).read_text(encoding="utf-8")


def test_a_path_gitattributes_governs_is_never_line_merged(tmp_path, monkeypatch):
    """`git merge-file` has no attribute or driver dispatch, so a `-merge` path
    line-merged here would apply exactly the policy the attribute forbids."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    (repo / ".gitattributes").write_text(f"{_NEW} -merge\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    assert port.port_relocations(repo, set()) == []
    assert git_out(repo, "diff", "--name-only", "--diff-filter=U") == _OLD


def test_a_configured_merge_driver_performs_the_port(tmp_path, monkeypatch):
    """`merge=<driver>` asks for a BETTER merge, not for no merge. The tree this
    resolver serves marks every `*.py` `merge=mergiraf`, which is the exact file
    class this port exists for, so refusing a named driver refused every real
    case. The driver runs over the rename's three blobs, and the port lands."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    (repo / ".gitattributes").write_text(
        f"{_OLD} merge=fake\n{_NEW} merge=fake\n", encoding="utf-8"
    )
    driver = repo / "driver.sh"
    driver.write_text(
        '#!/bin/sh\ngit merge-file "$2" "$1" "$3" || exit 1\n'
        'printf "# VIA DRIVER\\n" >> "$2"\n',
        encoding="utf-8",
    )
    driver.chmod(0o755)
    git_out(repo, "config", "merge.fake.driver", f"'{driver}' %O %A %B")
    monkeypatch.chdir(repo)

    done = port.port_relocations(repo, set())

    assert [(p.old_path, p.merged_clean) for p in done] == [(_OLD, True)]
    landed = (repo / _NEW).read_text(encoding="utf-8")
    assert "STRANDED EDIT" in landed, "the stranded edit must reach the destination"
    assert "# VIA DRIVER" in landed, "the repository's own driver must be what merged"
    assert git_out(repo, "diff", "--name-only", "--diff-filter=U") == ""


def test_the_stranded_sides_mode_change_reaches_the_destination(tmp_path, monkeypatch):
    """A real rename merge carries the stranded side's mode change onto the
    destination; the mover's mode alone would ship a non-executable script."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    git_out(repo, "merge", "--abort")
    git_out(repo, "checkout", "-q", "other")
    (repo / _OLD).chmod(0o755)
    commit_files(repo, {}, "make it executable")
    git_out(repo, "checkout", "-q", "main")
    subprocess.run(
        ["git", "merge", "--no-commit", "other"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        check=False,
    )
    monkeypatch.chdir(repo)

    assert port.port_relocations(repo, set())

    assert git_out(repo, "ls-files", "-s", "--", _NEW).split()[0] == "100755"


def test_two_paths_claiming_one_destination_port_neither(tmp_path, monkeypatch):
    """Each port reloads the mover blob, so the second would overwrite the first
    and drop its stranded edits. Nothing says which mapping is real."""
    repo = tmp_path / "repo"
    second = "sbx/other/egress_filter.py"
    init_test_repo(repo)
    commit_files(repo, {_OLD: _body(), second: _body()}, "two copies")
    git_out(repo, "checkout", "-q", "-b", "other")
    commit_files(
        repo,
        {_OLD: _body("# STRANDED A\n"), second: _body("# STRANDED B\n")},
        "edit both old paths",
    )
    git_out(repo, "checkout", "-q", "main")
    commit_files(
        repo, {_OLD: _LAUNCHER, second: _LAUNCHER, _NEW: _body()}, "consolidate"
    )
    subprocess.run(
        ["git", "merge", "--no-commit", "other"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        check=False,
    )
    monkeypatch.chdir(repo)

    assert port.port_relocations(repo, set()) == []
    assert _OLD in git_out(repo, "diff", "--name-only", "--diff-filter=U")


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
