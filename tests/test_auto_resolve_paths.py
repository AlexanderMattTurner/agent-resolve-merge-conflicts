"""One answer per question about a conflicted path.

Each case builds a real scratch repo, drives an actual merge through git, and
asks every reader of one path the same question. The assertions are that they
AGREE: a reader that answers alone looks correct on its own, so a test driving
one of them passes while the two verdicts disagree.
"""

import contextlib
import enum
import itertools
import os
import subprocess
import sys
import typing
from pathlib import Path

import pytest

from tests._helpers import commit_all, commit_files, git_env, git_out, init_test_repo
from tests._resolver_helpers import REPO_ROOT, load_script

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
    """A path bound to a driver other than mergiraf names that driver to every
    reader asked about it.

    A reader that missed it would call the path an ordinary text conflict and
    re-merge it, dropping the resolution git already applied.
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
    """`merge.default` binds a driver for every path with no attribute of its own,
    so a reader taking `git check-attr`'s `unspecified` at face value misses it."""
    repo = _conflicted(tmp_path, "", merge_default="ours")
    assert _facts(repo).policy is paths_mod.MergePolicy.DRIVER
    with _cwd(repo):
        assert port._effective_merge_attr(_PATH) == "ours"


@pytest.mark.parametrize("attribute", ["-merge", "merge=binary"])
def test_both_spellings_of_no_textual_merge_are_unmergeable(tmp_path, attribute):
    """`-merge` and the built-in `binary` driver mean the same thing to git: no
    markers, so no edit resolves the path. Both spellings reach the partition."""
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


_LIB = REPO_ROOT / ".github/resolver/auto-resolve/lib.sh"

#: Sources lib.sh and prints one `path NUL flag NUL yes|no NUL` record per pair,
#: so the answer read here is `has_fact`'s own exit status.
_SEAM_DRIVER = r"""
set -euo pipefail
source "$1"
root="$2"
owned="$3"
shift 3
IFS=' ' read -r -a flags <<<"$SEAM_FLAGS"
load_path_facts "$root" HEAD "$owned" "$@"
for path in "$@"; do
  for flag in "${flags[@]}"; do
    if has_fact "$path" "$flag"; then held=yes; else held=no; fi
    printf '%s\0%s\0%s\0' "$path" "$flag" "$held"
  done
done
"""

#: Each fixture path against the flags the SHELL asks `has_fact` for, spelled as
#: `prepare.sh` and `land.sh` spell them. Written out rather than taken from
#: `flags_of`, which is the reader under test: a renamed flag would rename the
#: expectation with it and the case would pass through the break.
_SEAM_EXPECTED = {
    "plain.md": set(),
    "odd name.md": set(),
    "kept.md": set(),
    "gone.md": {"modify_delete"},
    "added.md": {"add_add"},
    "sealed.md": {"unmergeable"},
    "blob.bin": {"unmergeable"},
    # Staged, so `git ls-files -u` no longer names it. Still unmergeable: the two
    # sides are what decide that, and a pass that stages a resolution does not
    # turn a binary into a file a model may edit.
    "staged.bin": {"unmergeable"},
    "vendor/uv.lock": {"generated_owned", "lockfile"},
}


def _flag_names() -> list[str]:
    """Every flag spelling `flags_of` can print, driven out of `flags_of` over
    the whole domain of `PathFacts`' fields.

    Enumerated rather than listed, so a flag added later is asked about here
    without an edit: a bool field contributes both answers and an enum field
    every member, and the union of what `flags_of` prints over that product is
    the whole vocabulary the shell can be asked for.
    """
    domains: dict[str, tuple] = {}
    for name, kind in typing.get_type_hints(paths_mod.PathFacts).items():
        if kind is bool:
            domains[name] = (True, False)
        elif isinstance(kind, type) and issubclass(kind, enum.Enum):
            domains[name] = tuple(kind)
        else:
            domains[name] = ("a/path",)
    names: set[str] = set()
    for combination in itertools.product(*domains.values()):
        facts = paths_mod.PathFacts(**dict(zip(domains, combination)))
        names.update(flag for flag in paths_mod.flags_of(facts).split(",") if flag)
    return sorted(names)


def _seam_repo(repo: Path) -> None:
    """A mid-merge repo holding one path per flag the shell routes on: a
    modify/delete, an add/add, a `-merge` file, two binaries, a caller-owned
    lockfile, a driver-resolved path and two ordinary text conflicts.

    One binary is staged after the merge, standing for a deterministic pass that
    resolved it before the classification runs."""
    init_test_repo(repo)
    text = ["plain.md", "odd name.md", "gone.md", "sealed.md", "kept.md"]
    binaries = ["blob.bin", "staged.bin"]
    commit_files(
        repo,
        {**{name: "base\n" for name in text}, "vendor/uv.lock": "version = 1\n"},
        "the base side",
    )
    for name in binaries:
        (repo / name).write_bytes(b"\x00base\n")
    commit_all(repo, "two files git reads as binary")

    git_out(repo, "checkout", "-q", "-b", "other")
    (repo / "gone.md").unlink()
    for name in binaries:
        (repo / name).write_bytes(b"\x00their side\n")
    commit_files(
        repo,
        {
            **{name: "their side\n" for name in text if name != "gone.md"},
            "vendor/uv.lock": "version = 3\n",
            "added.md": "their new file\n",
        },
        "their edit",
    )

    git_out(repo, "checkout", "-q", "main")
    for name in binaries:
        (repo / name).write_bytes(b"\x00our side\n")
    commit_files(
        repo,
        {
            **{name: "our side\n" for name in text},
            "vendor/uv.lock": "version = 2\n",
            "added.md": "our new file\n",
            ".gitattributes": "sealed.md -merge\nkept.md merge=ours\n",
        },
        "our edit",
    )
    subprocess.run(
        ["git", "merge", "--no-commit", "other"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        check=False,
    )
    git_out(repo, "add", "--", "staged.bin")


def test_the_shell_reads_back_every_flag_the_classifier_emits(tmp_path):
    """`has_fact` answers each flag exactly as `classify` decided it.

    The two are different languages either side of one byte stream: `flags_of`
    names the flags, `_emit` writes `path NUL answer NUL`, and `load_path_facts`
    parses that back with paired `read -r -d ""` reads. Every miss across that
    seam is fail-OPEN — a flag the shell cannot find reads as a flag the path
    does not hold, so a modify/delete (which carries no markers) goes to the
    ordinary marker prompt and a generator-owned path goes to the model.

    Both directions are asserted, per path and per flag, and the fixture holds a
    path for every flag the shell routes on.
    """
    repo = tmp_path / "seam"
    _seam_repo(repo)
    owned_file = tmp_path / "owned.txt"
    owned_file.write_text("vendor/\n", encoding="utf-8")
    # The interpreter running this test, so `python3` in lib.sh is the one the
    # expectations below are computed with rather than whatever the PATH holds.
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "python3").symlink_to(sys.executable)

    flags = _flag_names()
    assert flags, (
        "flags_of named no flag — every assertion below would pass over nothing"
    )
    paths = sorted(_SEAM_EXPECTED)
    done = subprocess.run(
        ["bash", "-c", _SEAM_DRIVER, "seam", str(_LIB), str(repo), str(owned_file)]
        + paths,
        cwd=repo,
        env={
            **git_env(),
            "PATH": f"{shim}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "SEAM_FLAGS": " ".join(flags),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr

    fields = done.stdout.split("\0")[:-1]
    read_back = {
        (path, flag): held == "yes"
        for path, flag, held in zip(fields[::3], fields[1::3], fields[2::3])
    }
    assert len(read_back) == len(paths) * len(flags)

    paths_mod.bind_repo(repo)
    owned = owned_mod.parse(owned_file.read_text(encoding="utf-8"))
    facts = paths_mod.classify(paths, base_remote_ref="HEAD", owned=owned)
    for path in paths:
        emitted = {f for f in paths_mod.flags_of(facts[path]).split(",") if f}
        for flag in flags:
            assert read_back[(path, flag)] == (flag in emitted), (
                f"has_fact '{path}' {flag} disagrees with classify"
            )
        held = {flag for flag in flags if read_back[(path, flag)]}
        assert held == _SEAM_EXPECTED[path], (
            f"the shell reads the wrong flags for {path}"
        )


def test_an_ownership_prefix_covers_a_file_under_it():
    """`--owned` prints a rule's owned DIRECTORY with a trailing slash. Exact
    equality alone would answer "not owned" for the whole tree."""
    owned = owned_mod.parse("exact.lock\nvendor/\n\n")
    assert owned.covers("exact.lock")
    assert owned.covers("vendor/uv.lock")
    assert not owned.covers("other/uv.lock")
