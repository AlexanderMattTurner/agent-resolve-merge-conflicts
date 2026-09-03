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

merge_attr = load_script(".github/resolver/auto-resolve/_merge_attr.py")
relocation = load_script(".github/resolver/auto-resolve/_relocation.py")
port = load_script(".github/resolver/auto-resolve/_relocation_port.py")

_OLD = "sbx/lib/egress_filter.py"
_NEW = "pkg/src/gw/egress_filter.py"
_LAUNCHER = '"""Launcher."""\n\nfrom gw.egress_filter import main\n'

_OLD_YAML = "sbx/lib/egress_policy.yaml"
_NEW_YAML = "pkg/src/gw/egress_policy.yaml"
_YAML_LAUNCHER = (
    "# The policy now lives beside the gateway.\ninclude: gw/egress_policy.yaml\n"
)


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


def _yaml_body(tail: str = "# tail\n") -> str:
    lines = [
        f"  - name: rule_{n}\n    upstream: registry-{n}.example.invalid\n"
        f"    action: allow-with-a-distinctive-value-{n}"
        for n in range(60)
    ]
    return "rules:\n" + "\n".join(lines) + "\n" + tail


def _repo(
    tmp_path: Path,
    *,
    mover_is_head: bool,
    mover_tail: str,
    old: str = _OLD,
    new: str = _NEW,
    launcher: str = _LAUNCHER,
    body=_body,
) -> Path:
    """A mid-merge repo where one side moved the body and the other edited the
    old path's tail. `mover_is_head` flips which side git calls ours. `old`,
    `new`, `launcher` and `body` name the file type, which is what decides the
    destination's merge attribute."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {old: body()}, "add the filter")
    git_out(repo, "checkout", "-q", "-b", "other")
    commit_files(repo, {old: body("# STRANDED EDIT\n")}, "edit the old path")
    git_out(repo, "checkout", "-q", "main")
    commit_files(repo, {old: launcher, new: body(mover_tail)}, "move into a package")
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
    and every existing guard applies to it unchanged.

    Including the SHAPE of the block. `git merge-file` reads no configuration, so
    it writes the plain two-section style whatever `merge.conflictStyle` says,
    and a ported file used to be the one conflict in the tree with no base
    section — the section mergiraf rebuilds from and the model's prompt describes.
    """
    repo = _repo(
        tmp_path, mover_is_head=mover_is_head, mover_tail="# MOVER CHANGED THIS\n"
    )
    monkeypatch.chdir(repo)

    _moved, ported = _port_one(repo)

    assert not ported.merged_clean
    assert git_out(repo, "diff", "--name-only", "--diff-filter=U") == _NEW
    landed = (repo / _NEW).read_text(encoding="utf-8")
    assert "<<<<<<<" in landed
    assert "|||||||" in landed, "the ported block carries its merge base"
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


def test_a_structurally_unsafe_destination_is_line_merged(tmp_path, monkeypatch):
    """The syntax-aware driver DROPS content on a `.yaml`, `.yml` or `.toml`
    file, so the resolver unbinds it for those types in the checkout that runs
    the merge. The port merges the way that checkout does, which makes a `.yaml`
    destination bound to that driver a LINE merge here. Running the driver would
    apply to the rename the one merge the repository's own merge never gets."""
    repo = _repo(
        tmp_path,
        mover_is_head=True,
        mover_tail="# tail\n",
        old=_OLD_YAML,
        new=_NEW_YAML,
        launcher=_YAML_LAUNCHER,
        body=_yaml_body,
    )
    structural = merge_attr.STRUCTURAL_DRIVER
    (repo / ".gitattributes").write_text(
        f"*.yaml merge={structural}\n", encoding="utf-8"
    )
    ran = tmp_path / "structural-driver-ran"
    driver = repo / "driver.sh"
    driver.write_text(f'#!/bin/sh\n: > "{ran}"\nexit 0\n', encoding="utf-8")
    driver.chmod(0o755)
    git_out(repo, "config", f"merge.{structural}.driver", f"'{driver}' %O %A %B")
    monkeypatch.chdir(repo)

    done = port.port_relocations(repo, set())

    assert [(p.old_path, p.merged_clean) for p in done] == [(_OLD_YAML, True)]
    assert not ran.exists(), f"`{structural}` must not merge a .yaml destination"
    landed = (repo / _NEW_YAML).read_text(encoding="utf-8")
    assert "STRANDED EDIT" in landed, "the stranded edit must reach the destination"


def _scratch_with_three_blobs(where: Path) -> Path:
    """The three files `_run_driver` hands a driver as `%O`, `%A` and `%B`."""
    where.mkdir(parents=True, exist_ok=True)
    for name in ("base", "mover", "stranded"):
        (where / name).write_text("body\n", encoding="utf-8")
    return where


def test_a_hostile_destination_path_never_reaches_the_shell_as_code(tmp_path):
    """`%P` is a path the merged branch chose, and the driver line runs through a
    shell. Unquoted, a filename holding `";cmd;"` executes cmd in a privileged CI
    job. git quotes each value with `sq_quote_buf` before expanding it; so does
    this. The driver must SEE the name and the shell must never run it."""
    scratch = _scratch_with_three_blobs(tmp_path / "scratch")
    pwned = tmp_path / "pwned"
    seen = tmp_path / "seen"
    hostile = f'pkg/a";touch {pwned};"b.py'

    content, clean = port._run_driver(  # noqa: SLF001
        f"printf '%s' %P > {seen}", _moved(_OLD, hostile), scratch
    )

    assert not pwned.exists(), "the destination path's shell metacharacters ran"
    assert seen.read_text(encoding="utf-8") == hostile
    assert (content, clean) == (b"body\n", True)


def test_the_labels_git_expands_are_substituted_and_the_paths_survive_a_space(
    tmp_path,
):
    """git expands `%S`, `%X` and `%Y` too; a table that drops them hands the
    driver the literal token. And `%O`/`%A`/`%B` are absolute, so an unquoted one
    splits into two arguments under a directory whose name holds a space."""
    scratch = _scratch_with_three_blobs(tmp_path / "with a space" / "scratch")
    argv = tmp_path / "argv"
    driver = tmp_path / "driver.sh"
    driver.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$RECORD"\n', "utf-8")
    driver.chmod(0o755)
    moved = _moved(_OLD, _NEW)

    port._run_driver(  # noqa: SLF001
        f"RECORD={argv} '{driver}' %O %A %B %S %X %Y", moved, scratch
    )

    assert argv.read_text(encoding="utf-8").splitlines() == [
        str(scratch / "base"),
        str(scratch / "mover"),
        str(scratch / "stranded"),
        f"{_OLD} (merge base)",
        _NEW,
        f"{_OLD} (the base branch)",
    ]


def test_a_driver_that_conflicts_leaves_the_destination_unmerged(tmp_path, monkeypatch):
    """A driver signals remaining conflicts by exiting non-zero, and its result is
    in `%A`. The port must carry that result onto the destination and leave it
    genuinely unmerged, never report the port clean."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    (repo / ".gitattributes").write_text(f"{_NEW} merge=fake\n", encoding="utf-8")
    driver = repo / "driver.sh"
    driver.write_text(
        "#!/bin/sh\n"
        'printf "<<<<<<< ours\\n# DRIVER CONFLICT\\n=======\\n>>>>>>> theirs\\n"'
        ' >> "$2"\nexit 1\n',
        encoding="utf-8",
    )
    driver.chmod(0o755)
    git_out(repo, "config", "merge.fake.driver", f"'{driver}' %O %A %B")
    monkeypatch.chdir(repo)

    done = port.port_relocations(repo, set())

    assert [(p.old_path, p.merged_clean) for p in done] == [(_OLD, False)]
    assert git_out(repo, "diff", "--name-only", "--diff-filter=U") == _NEW
    assert "# DRIVER CONFLICT" in (repo / _NEW).read_text(encoding="utf-8")
    stages = git_out(repo, "ls-files", "-u", "--", _NEW).split("\n")
    assert len(stages) == 3, "the destination carries all three merge stages"


def test_a_driver_the_shell_cannot_run_refuses_and_says_why(
    tmp_path, monkeypatch, capsys
):
    """127 is `command not found`, which read as "127 conflicts" would stage a
    destination nothing merged. The refusal must carry the driver's own stderr,
    which is the one line naming the missing binary."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    (repo / ".gitattributes").write_text(f"{_NEW} merge=fake\n", encoding="utf-8")
    git_out(repo, "config", "merge.fake.driver", "no-such-merge-driver %O %A %B")
    monkeypatch.chdir(repo)
    before = git_out(repo, "ls-files", "-s", "-u")

    assert port.port_relocations(repo, set()) == []

    assert git_out(repo, "ls-files", "-s", "-u") == before
    warning = capsys.readouterr().err
    assert "exited 127" in warning
    assert "no-such-merge-driver" in warning, "the driver's own stderr must survive"


def test_a_driver_that_never_finishes_refuses(tmp_path, monkeypatch):
    """A merge driver is arbitrary code from the repository's own config. One
    that blocks forever would hang the whole resolver run, so the port refuses
    and leaves the merge exactly as git wrote it."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    (repo / ".gitattributes").write_text(f"{_NEW} merge=fake\n", encoding="utf-8")
    git_out(repo, "config", "merge.fake.driver", "sleep 30")
    monkeypatch.setattr(port, "_DRIVER_TIMEOUT_SECONDS", 1)
    monkeypatch.chdir(repo)
    before = git_out(repo, "ls-files", "-s", "-u")

    with pytest.raises(port.PortRefused, match="did not finish"):
        _port_one(repo)

    assert git_out(repo, "ls-files", "-s", "-u") == before


def test_an_unspecified_merge_attribute_takes_merge_default(tmp_path, monkeypatch):
    """gitattributes(5): an unspecified `merge` takes the `merge.default` driver,
    not the built-in text merge. A consumer that binds one repo-wide and writes
    no per-path attribute is the same failure, reached the other way."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    driver = repo / "driver.sh"
    driver.write_text(
        '#!/bin/sh\ngit merge-file "$2" "$1" "$3" || exit 1\n'
        'printf "# VIA DEFAULT\\n" >> "$2"\n',
        encoding="utf-8",
    )
    driver.chmod(0o755)
    git_out(repo, "config", "merge.default", "fake")
    git_out(repo, "config", "merge.fake.driver", f"'{driver}' %O %A %B")
    monkeypatch.chdir(repo)

    done = port.port_relocations(repo, set())

    assert [(p.old_path, p.merged_clean) for p in done] == [(_OLD, True)]
    landed = (repo / _NEW).read_text(encoding="utf-8")
    assert "STRANDED EDIT" in landed
    assert "# VIA DEFAULT" in landed, "merge.default must decide an unspecified path"


def test_an_unbound_merge_default_still_lands_on_the_text_merge(tmp_path, monkeypatch):
    """The fallback must not turn every ordinary path into a refusal: with no
    `merge.default` configured, an unspecified attribute is the text merge."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    monkeypatch.chdir(repo)

    assert port._effective_merge_attr(_NEW) == "text"  # noqa: SLF001


def test_the_driver_gets_ours_as_A_whichever_side_relocated(tmp_path, monkeypatch):
    """git's contract binds `%A` to the CURRENT side and `%B` to the other one,
    by index stage, never by which side did the relocating. A driver that keeps
    `%A` unconditionally must therefore keep the current branch's content. Here
    the mover is MERGE_HEAD, so ours is the STRANDED side."""
    repo = _repo(tmp_path, mover_is_head=False, mover_tail="# MOVER TAIL\n")
    (repo / ".gitattributes").write_text(f"{_NEW} merge=fake\n", encoding="utf-8")
    git_out(repo, "config", "merge.fake.driver", "true")
    monkeypatch.chdir(repo)

    done = port.port_relocations(repo, set())

    assert [(p.old_path, p.merged_clean) for p in done] == [(_OLD, True)]
    landed = (repo / _NEW).read_text(encoding="utf-8")
    assert "# STRANDED EDIT" in landed, "`%A` must be the current side, not the mover"
    assert "# MOVER TAIL" not in landed


def test_a_conflicting_driver_that_wrote_no_markers_is_refused(
    tmp_path, monkeypatch, capsys
):
    """A driver signals conflicts by its exit status alone; markers are not part
    of the contract. Staging a markerless result as unmerged leaves a worktree
    that reads resolved, so a later pass commits one side and silently drops the
    other's edits. Refuse before touching the index."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    (repo / ".gitattributes").write_text(f"{_NEW} merge=fake\n", encoding="utf-8")
    git_out(repo, "config", "merge.fake.driver", "exit 1")
    monkeypatch.chdir(repo)
    before = git_out(repo, "ls-files", "-s", "-u")

    assert port.port_relocations(repo, set()) == []

    assert git_out(repo, "ls-files", "-s", "-u") == before
    assert "left no conflict markers" in capsys.readouterr().err


def test_the_driver_gets_the_paths_own_conflict_marker_size(tmp_path, monkeypatch):
    """`%L` is the destination's `conflict-marker-size`, which a repository raises
    for a file whose own content holds `<<<<<<<` lines. A hard-coded 7 hands the
    driver a size the real merge would never have used."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    (repo / ".gitattributes").write_text(
        f"{_NEW} merge=fake conflict-marker-size=16\n", encoding="utf-8"
    )
    seen = tmp_path / "marker-size"
    driver = repo / "driver.sh"
    driver.write_text(f'#!/bin/sh\nprintf "%s" "$1" > {seen}\n', encoding="utf-8")
    driver.chmod(0o755)
    git_out(repo, "config", "merge.fake.driver", f"'{driver}' %L")
    monkeypatch.chdir(repo)

    assert port.port_relocations(repo, set())

    assert seen.read_text(encoding="utf-8") == "16"


def test_an_empty_driver_command_refuses_instead_of_line_merging(
    tmp_path, monkeypatch, capsys
):
    """An explicitly empty `merge.<name>.driver` is a merge git FAILS, leaving the
    path conflicted. Collapsing it to `None` line-merges a path the repository's
    own configuration would never have merged."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# tail\n")
    (repo / ".gitattributes").write_text(f"{_NEW} merge=fake\n", encoding="utf-8")
    git_out(repo, "config", "merge.fake.driver", "")
    monkeypatch.chdir(repo)

    assert port.port_relocations(repo, set()) == []

    assert "empty command" in capsys.readouterr().err


def test_a_config_lookup_that_fails_refuses_instead_of_line_merging(monkeypatch):
    """Only the ABSENT key — `git config --get` exiting 1 — is git's text-merge
    fallback. A lookup that failed any other way did not answer, so reading it as
    "no driver" line-merges on an answer nobody gave.

    The failure is injected rather than induced: every real way to make `git
    config` fail (a malformed config file, an unset `GIT_CONFIG_KEY_0`) fails
    every other git command too, so `_merge_attr`'s `check-attr` refuses first
    and this branch is never reached."""
    real = port.run_git

    def failing(*args):
        if args[:3] == ("config", "--get", "merge.fake.driver"):
            return subprocess.CompletedProcess(args, 128, "", "fatal: bad config\n")
        return real(*args)

    monkeypatch.setattr(port, "run_git", failing)

    with pytest.raises(port.PortRefused, match="could not read `merge.fake.driver`"):
        port._driver_command("fake", _NEW)  # noqa: SLF001


def test_a_union_path_keeps_both_sides_instead_of_conflicting(tmp_path, monkeypatch):
    """`union` is one of git's three BUILT-IN drivers, so no `merge.union.driver`
    exists to find. Falling through to the text merge hands a `merge=union` path
    conflict markers where git would have kept both sides' lines."""
    repo = _repo(tmp_path, mover_is_head=True, mover_tail="# MOVER CHANGED THIS\n")
    (repo / ".gitattributes").write_text(f"{_NEW} merge=union\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    done = port.port_relocations(repo, set())

    assert [(p.old_path, p.merged_clean) for p in done] == [(_OLD, True)]
    landed = (repo / _NEW).read_text(encoding="utf-8")
    assert "<<<<<<<" not in landed, "union never writes conflict markers"
    assert "# MOVER CHANGED THIS" in landed
    assert "# STRANDED EDIT" in landed


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
