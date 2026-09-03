"""The conflict ledger, driven against REAL merges git produced.

Each case builds a scratch repository whose merge leaves all three conflict
shapes at once — a both-modified file, an add/add file, and a modify/delete
file — plus one path whose name holds a space, a tab, a newline and a non-ASCII
character. Nothing stubs git: the stages under test are the ones git's own index
recorded.

`tests/_conflict_ledger.py` loads the module under test, standing `_paths.py` in
while that sibling is absent.
"""

# covers: .github/resolver/auto-resolve/_conflict_set.py

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests._conflict_ledger import conflict_set
from tests._helpers import commit_files, git_env, git_out, init_test_repo

# A space, a tab, a newline and a non-ASCII character in one name: the four
# things prepare.sh's whitespace-joined step outputs cannot carry.
ODD = "od d\tname\nünicode.txt"

_BASE = "".join(f"line {n}\n" for n in range(8))

git_io = sys.modules["_git_io"]

Claimed = conflict_set.Claimed
Disposition = conflict_set.Disposition
Shape = conflict_set.Shape

STAGED = Disposition(claimed=Claimed.STAGED, by="mergiraf")
REFUSED = Disposition(claimed=Claimed.REFUSED, by="prepare", reason="binary")
TO_MODEL = Disposition(claimed=Claimed.TO_MODEL, by="prepare", prompt="marker")


@pytest.fixture(autouse=True)
def _unbind_git_io():
    """`_git_io` holds the bound repository in a module global, and the worker
    imports it once for the whole file. Give the binding back, so a later test
    cannot act on a scratch repo this one already removed."""
    yield
    git_io._reset_process_state()


def _conflicted_repo(tmp_path: Path) -> Path:
    """A mid-merge repository holding one conflict of each shape.

    `both.txt` and ODD are edited on both sides, `added.txt` is added on both
    sides, and `gone.txt` is edited here and deleted there.
    """
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {"both.txt": _BASE, ODD: _BASE, "gone.txt": _BASE}, "base")

    git_out(repo, "checkout", "-q", "-b", "other")
    (repo / "gone.txt").unlink()
    commit_files(
        repo,
        {"both.txt": "theirs\n", ODD: "theirs\n", "added.txt": "theirs\n"},
        "their side",
    )

    git_out(repo, "checkout", "-q", "main")
    commit_files(
        repo,
        {
            "both.txt": "ours\n",
            ODD: "ours\n",
            "added.txt": "ours\n",
            "gone.txt": _BASE + "ours\n",
        },
        "our side",
    )
    merge = subprocess.run(
        ["git", "merge", "--no-edit", "other"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert merge.returncode != 0, f"the fixture merge did not conflict: {merge.stdout}"
    return repo


def _ledger(tmp_path: Path):
    """The ledger of the fixture merge, with git bound to that repository."""
    git_io.bind_repo(_conflicted_repo(tmp_path))
    return conflict_set.ConflictSet.from_index(base_remote_ref="other", owned=set())


def test_every_shape_comes_from_the_stages_git_recorded(tmp_path):
    ledger = _ledger(tmp_path)
    paths = [entry.path for entry in ledger.entries()]

    assert paths == sorted([ODD, "added.txt", "both.txt", "gone.txt"])
    assert {entry.path: Shape.of(entry.stages) for entry in ledger.entries()} == {
        "both.txt": Shape.BOTH_MODIFIED,
        ODD: Shape.BOTH_MODIFIED,
        "added.txt": Shape.ADD_ADD,
        "gone.txt": Shape.MODIFY_DELETE,
    }
    # The shape is a fact about which stages git wrote, not a label beside them.
    assert ledger.entry("added.txt").stages.base is None
    assert ledger.entry("gone.txt").stages.theirs is None
    assert ledger.entry("gone.txt").stages.ours is not None
    assert ledger.partition(Claimed.UNCLAIMED) == paths
    with pytest.raises(KeyError):
        ledger.entry("never-conflicted.txt")


@pytest.mark.parametrize(
    "first", [STAGED, REFUSED, TO_MODEL], ids=["staged", "refused", "to_model"]
)
def test_a_terminal_claim_refuses_every_later_pass(tmp_path, first):
    ledger = _ledger(tmp_path)
    ledger.claim("both.txt", by=first.by, disposition=first)

    later = Disposition(claimed=Claimed.STAGED, by="llm")
    with pytest.raises(conflict_set.ClaimConflict) as refusal:
        ledger.claim("both.txt", by="llm", disposition=later)

    assert "both.txt" in str(refusal.value)
    assert ledger.entry("both.txt").disposition == first


def test_a_deferred_path_is_finished_only_by_the_pass_it_names(tmp_path):
    ledger = _ledger(tmp_path)
    handoff = Disposition(claimed=Claimed.DEFERRED, by="prepare", to="bundle")
    ledger.claim("both.txt", by="prepare", disposition=handoff)

    intruder = Disposition(claimed=Claimed.STAGED, by="mergiraf")
    with pytest.raises(conflict_set.ClaimConflict) as refusal:
        ledger.claim("both.txt", by="mergiraf", disposition=intruder)
    assert "bundle" in str(refusal.value)
    assert ledger.entry("both.txt").disposition == handoff

    finished = Disposition(claimed=Claimed.STAGED, by="bundle")
    ledger.claim("both.txt", by="bundle", disposition=finished)
    assert ledger.partition(Claimed.STAGED) == ["both.txt"]
    assert ledger.partition(Claimed.DEFERRED) == []


def test_the_driver_refuses_while_any_path_is_unjudged(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.claim("both.txt", by="mergiraf", disposition=STAGED)

    with pytest.raises(conflict_set.UnclaimedPaths) as refusal:
        ledger.require_fully_dispositioned()
    assert "added.txt" in str(refusal.value)
    assert "both.txt" not in str(refusal.value)

    for path in ledger.partition(Claimed.UNCLAIMED):
        ledger.claim(path, by="prepare", disposition=REFUSED)
    assert ledger.require_fully_dispositioned() is None


def test_json_carries_a_whitespace_and_unicode_path_byte_for_byte(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.claim(ODD, by="prepare", disposition=TO_MODEL)
    ledger.claim("gone.txt", by="prepare", disposition=REFUSED)

    text = ledger.to_json()
    restored = conflict_set.ConflictSet.from_json(text)

    assert restored.entries() == ledger.entries()
    assert restored.entry(ODD).path == ODD
    assert restored.partition(Claimed.TO_MODEL) == [ODD]
    decoded = {entry["path"]: entry for entry in json.loads(text)["entries"]}
    assert decoded[ODD]["disposition"]["prompt"] == "marker"
    # ASCII on the wire, so no step in the chain has to agree on a locale.
    assert text.isascii()


def test_a_disposition_holds_only_the_field_its_state_owns():
    with pytest.raises(ValueError):
        Disposition(claimed=Claimed.DEFERRED, by="prepare")
    with pytest.raises(ValueError):
        Disposition(claimed=Claimed.REFUSED, by="prepare", reason="x", to="bundle")
    with pytest.raises(ValueError):
        Disposition(claimed=Claimed.TO_MODEL, by="prepare", prompt="guesswork")
    with pytest.raises(ValueError):
        Disposition(claimed=Claimed.UNCLAIMED, by="prepare")


def test_the_build_cli_prints_the_whole_ledger(tmp_path, monkeypatch, capsys):
    repo = _conflicted_repo(tmp_path)
    owned_file = tmp_path / "owned.txt"
    owned_file.write_text("both.txt\n\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    conflict_set.main(
        ["--build", "--base-ref", "other", "--owned-file", str(owned_file)]
    )

    ledger = conflict_set.ConflictSet.from_json(capsys.readouterr().out)
    paths = [entry.path for entry in ledger.entries()]
    assert paths == sorted([ODD, "added.txt", "both.txt", "gone.txt"])
    assert ledger.partition(Claimed.UNCLAIMED) == paths
