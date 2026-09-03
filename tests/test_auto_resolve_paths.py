"""One answer per question about a conflicted path.

Each case builds a real scratch repo, drives an actual merge through git, and
asks the readers that used to answer separately. The assertions are that they
AGREE — the divergences these close were silent, so a test that only drove one
reader would have passed throughout.
"""

import contextlib
import os
import subprocess
from pathlib import Path

import pytest

from tests._helpers import commit_files, git_env, git_out, init_test_repo
from tests._resolver_helpers import load_script

owned_mod = load_script(".github/resolver/auto-resolve/_owned.py")
paths_mod = load_script(".github/resolver/auto-resolve/_paths.py")
lockfiles = load_script(".github/resolver/auto-resolve/_lockfiles.py")
port = load_script(".github/resolver/auto-resolve/_relocation_port.py")

_PATH = "docs/table.md"


@contextlib.contextmanager
def _cwd(path: Path):
    """Run a block with the process working directory at PATH.

    `_relocation_port` reaches git through the process working directory, which
    is the checkout its step owns; the readers beside it bind a repository
    explicitly. Both are asked here, so the test gives each what it reads.
    """
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _conflicted(tmp_path: Path, attributes: str, *, merge_default: str = "") -> Path:
    """A mid-merge repo whose two sides both edited `_PATH`, with `.gitattributes`
    committed on the BASE side — which is where every reader now takes it from."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_PATH: "base\n"}, "add the file")
    git_out(repo, "checkout", "-q", "-b", "other")
    commit_files(repo, {_PATH: "their side\n"}, "their edit")
    git_out(repo, "checkout", "-q", "main")
    files = {_PATH: "our side\n"}
    if attributes:
        files[".gitattributes"] = attributes
    commit_files(repo, files, "our edit")
    if merge_default:
        git_out(repo, "config", "merge.default", merge_default)
    subprocess.run(
        ["git", "merge", "--no-commit", "other"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        check=False,
    )
    paths_mod.bind_repo(repo)
    return repo


def _facts(repo: Path, path: str = _PATH, owned=None):
    return paths_mod.classify(
        [path],
        base_remote_ref="HEAD",
        owned=owned if owned is not None else owned_mod.EMPTY,
    )[path]


def test_a_named_driver_gets_one_verdict_from_every_reader(tmp_path):
    """A path bound to a driver other than mergiraf: the partition, the
    relocation port and the table pre-pass each used to answer differently.

    The port refused it, the partition called it an ordinary text conflict, and
    only the port resolved `merge.default` at all. All three now read
    `_merge_attr`, so the driver is visible wherever the path is asked about.
    """
    repo = _conflicted(tmp_path, f"{_PATH} merge=ours\n")
    facts = _facts(repo)
    assert facts.policy is paths_mod.MergePolicy.DRIVER
    # Not unmergeable: git applied the driver, so the file has a resolution — it
    # is simply not one this resolver's own passes may recompute.
    assert not facts.unmergeable
    with _cwd(repo):
        assert port._effective_merge_attr(_PATH) == "ours"


def test_a_repo_wide_merge_default_reaches_the_partition(tmp_path):
    """`merge.default` binds a driver for every path with no attribute of its own.
    The raw readers saw `unspecified` and called it a plain text merge."""
    repo = _conflicted(tmp_path, "", merge_default="ours")
    assert _facts(repo).policy is paths_mod.MergePolicy.DRIVER
    with _cwd(repo):
        assert port._effective_merge_attr(_PATH) == "ours"


@pytest.mark.parametrize("attribute", ["-merge", "merge=binary"])
def test_both_spellings_of_no_textual_merge_are_unmergeable(tmp_path, attribute):
    """`-merge` and the built-in `binary` driver mean the same thing to git: no
    markers, so no edit resolves it. Only the first used to reach the partition."""
    repo = _conflicted(tmp_path, f"{_PATH} {attribute}\n")
    facts = _facts(repo)
    assert facts.policy is paths_mod.MergePolicy.UNMERGEABLE
    assert facts.unmergeable


def test_a_caller_owned_lockfile_keeps_one_classification(tmp_path):
    """A lockfile the CALLER's rule table owns is both `generated_owned` and a
    recognized `lockfile`. The routing pass and the partition must read the same
    two answers: one that saw only ownership would hand it to the built-in lock
    command, and one that saw only the registry would keep it out of the
    caller's own re-derivation."""
    repo = tmp_path / "caller-owned"
    init_test_repo(repo)
    commit_files(repo, {"vendor/uv.lock": "version = 1\n"}, "a vendored lockfile")
    paths_mod.bind_repo(repo)
    owned = owned_mod.parse("vendor/\n")
    facts = _facts(repo, "vendor/uv.lock", owned=owned)
    assert facts.generated_owned
    assert facts.lockfile
    verdict = lockfiles._route_one("vendor/uv.lock", str(repo), owned, set())
    assert verdict == "caller-owned\tvendor/uv.lock"


def test_the_structural_driver_reads_the_same_in_every_job(tmp_path):
    """prepare unbinds the syntax-aware driver from YAML and TOML by writing
    `$GIT_DIR/info/attributes`, which `git check-attr` reads ABOVE everything and
    no flag excludes — `--source=REF` replaces only the in-tree file. That file
    lives in prepare's own ephemeral checkout, so land's job reads `mergiraf`
    where prepare read `text`: one path, two policies, decided by which job asked.
    """
    repo = _conflicted(tmp_path, "*.yaml merge=mergiraf\n")
    land_job = _facts(repo, "a.yaml").policy

    info = repo / git_out(repo, "rev-parse", "--git-common-dir") / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "attributes").write_text('"a.yaml" merge=text\n', encoding="utf-8")
    prepare_job = _facts(repo, "a.yaml").policy

    assert land_job is prepare_job is paths_mod.MergePolicy.PLAIN
    with _cwd(repo):
        assert port._effective_merge_attr("a.yaml") == "text"


def test_an_ownership_prefix_covers_a_file_under_it():
    """`--owned` prints a rule's owned DIRECTORY with a trailing slash. Exact
    equality alone would answer "not owned" for the whole tree."""
    owned = owned_mod.parse("exact.lock\nvendor/\n\n")
    assert owned.covers("exact.lock")
    assert owned.covers("vendor/uv.lock")
    assert not owned.covers("other/uv.lock")
