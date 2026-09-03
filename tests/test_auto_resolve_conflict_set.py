"""The conflict ledger, driven against REAL merges git produced.

Most cases build a scratch repository whose merge leaves all three conflict
shapes at once — a both-modified file, an add/add file, and a modify/delete
file — plus one path whose name holds a space, a tab, a newline and a non-ASCII
character. One builds a rename/rename(1-to-2) instead, for the three paths git
leaves carrying a single index stage. Nothing stubs git: the stages under test
are the ones git's own index recorded.

`tests/_conflict_ledger.py` loads the module under test, with the one `_paths`
the FSM model beside it also reads.
"""

# covers: .github/resolver/auto-resolve/_conflict_set.py

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests._conflict_ledger import conflict_set, paths as paths_module
from tests._helpers import commit_files, git_env, git_out, init_test_repo

# A space, a tab, a newline and a non-ASCII character in one name: the four
# things prepare.sh's whitespace-joined step outputs cannot carry.
ODD = "od d\tname\nünicode.txt"

_BASE = "".join(f"line {n}\n" for n in range(8))

git_io = sys.modules["_git_io"]

Claimed = conflict_set.Claimed
Disposition = conflict_set.Disposition
Shape = paths_module.Shape

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


def _rename_split_repo(tmp_path: Path) -> Path:
    """A mid-merge repository whose one conflict is a rename/rename(1-to-2).

    Each side renames `orig.txt` to a different name, and git records three
    single-stage entries: `orig.txt` at stage 1 alone, `ourname.txt` at stage 2
    alone, `theirname.txt` at stage 3 alone. No other merge writes a stage set
    with one stage in it.
    """
    repo = tmp_path / "split"
    init_test_repo(repo)
    commit_files(repo, {"orig.txt": _BASE}, "base")

    git_out(repo, "checkout", "-q", "-b", "other")
    git_out(repo, "mv", "orig.txt", "theirname.txt")
    git_out(repo, "commit", "-q", "-m", "their rename")

    git_out(repo, "checkout", "-q", "main")
    git_out(repo, "mv", "orig.txt", "ourname.txt")
    git_out(repo, "commit", "-q", "-m", "our rename")
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
    return conflict_set.ConflictSet.from_index(base_remote_ref="other")


def test_every_shape_comes_from_the_stages_git_recorded(tmp_path):
    ledger = _ledger(tmp_path)
    paths = [entry.path for entry in ledger.entries()]

    assert paths == sorted([ODD, "added.txt", "both.txt", "gone.txt"])
    assert {entry.path: entry.stages.shape for entry in ledger.entries()} == {
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


def test_a_single_stage_path_reaches_the_ledger_instead_of_aborting_it(tmp_path):
    """A rename/rename(1-to-2) is a merge the resolver meets in the wild, and it
    leaves three paths carrying one index stage each. The ledger records what
    git wrote for them; refusing any one of them would take the whole merge down
    from `from_index`, so none of the three paths would ever be judged."""
    git_io.bind_repo(_rename_split_repo(tmp_path))
    ledger = conflict_set.ConflictSet.from_index(base_remote_ref="other")

    assert [entry.path for entry in ledger.entries()] == [
        "orig.txt",
        "ourname.txt",
        "theirname.txt",
    ]
    held = {
        entry.path: (
            entry.stages.base is not None,
            entry.stages.ours is not None,
            entry.stages.theirs is not None,
        )
        for entry in ledger.entries()
    }
    assert held == {
        "orig.txt": (True, False, False),
        "ourname.txt": (False, True, False),
        "theirname.txt": (False, False, True),
    }
    assert {entry.path: entry.facts.shape for entry in ledger.entries()} == {
        "orig.txt": Shape.BOTH_DELETED,
        "ourname.txt": Shape.ADDED_BY_US,
        "theirname.txt": Shape.ADDED_BY_THEM,
    }
    assert ledger.partition(Claimed.UNCLAIMED) == sorted(held)


@pytest.mark.parametrize(
    "first", [STAGED, REFUSED, TO_MODEL], ids=["staged", "refused", "to_model"]
)
def test_a_terminal_claim_refuses_every_later_pass(tmp_path, first):
    ledger = _ledger(tmp_path)
    ledger.claim("both.txt", disposition=first)

    later = Disposition(claimed=Claimed.STAGED, by="llm")
    with pytest.raises(conflict_set.ClaimConflict) as refusal:
        ledger.claim("both.txt", disposition=later)

    assert "both.txt" in str(refusal.value)
    assert ledger.entry("both.txt").disposition == first


def test_a_deferred_path_is_finished_only_by_the_pass_it_names(tmp_path):
    ledger = _ledger(tmp_path)
    handoff = Disposition(claimed=Claimed.DEFERRED, by="prepare", to="bundle")
    ledger.claim("both.txt", disposition=handoff)

    intruder = Disposition(claimed=Claimed.STAGED, by="mergiraf")
    with pytest.raises(conflict_set.ClaimConflict) as refusal:
        ledger.claim("both.txt", disposition=intruder)
    assert "bundle" in str(refusal.value)
    assert ledger.entry("both.txt").disposition == handoff

    finished = Disposition(claimed=Claimed.STAGED, by="bundle")
    ledger.claim("both.txt", disposition=finished)
    assert ledger.partition(Claimed.STAGED) == ["both.txt"]
    assert ledger.partition(Claimed.DEFERRED) == []


def test_the_driver_refuses_while_any_path_is_unjudged(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.claim("both.txt", disposition=STAGED)

    with pytest.raises(conflict_set.UnclaimedPaths) as refusal:
        ledger.require_fully_dispositioned()
    assert "added.txt" in str(refusal.value)
    assert "both.txt" not in str(refusal.value)

    for path in ledger.partition(Claimed.UNCLAIMED):
        ledger.claim(path, disposition=REFUSED)
    assert ledger.require_fully_dispositioned() is None


def test_json_carries_a_whitespace_and_unicode_path_byte_for_byte(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.claim(ODD, disposition=TO_MODEL)
    ledger.claim("gone.txt", disposition=REFUSED)

    text = ledger.to_json()
    restored = conflict_set.ConflictSet.from_json(text)

    assert restored.entries() == ledger.entries()
    # Identity, not equality, once per enum `from_json` re-coerces. A StrEnum
    # member equals its own text, so an `==` here would hold on the decoded
    # string too and the coercion could be dropped with every case still green.
    restored_odd = restored.entry(ODD)
    assert restored_odd.facts.shape is Shape.BOTH_MODIFIED
    assert restored_odd.facts.policy is paths_module.MergePolicy.PLAIN
    assert restored_odd.disposition.claimed is Claimed.TO_MODEL
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


def test_the_cli_prints_a_bucket_per_path_and_writes_the_routed_ledger(
    tmp_path, monkeypatch, capsys
):
    repo = _conflicted_repo(tmp_path)
    owned_file = tmp_path / "owned.txt"
    owned_file.write_text("both.txt\n\n", encoding="utf-8")
    ledger_out = tmp_path / "ledger.json"
    monkeypatch.chdir(repo)

    conflict_set.main(
        [
            "--base-ref",
            "other",
            "--owned-file",
            str(owned_file),
            "--ledger-out",
            str(ledger_out),
        ]
    )

    # NUL-terminated `path` then `bucket`, which is how prepare.sh reads a name
    # holding whitespace back whole.
    fields = capsys.readouterr().out.split("\0")[:-1]
    routed = dict(zip(fields[::2], fields[1::2], strict=True))
    paths = sorted([ODD, "added.txt", "both.txt", "gone.txt"])
    assert sorted(routed) == paths
    # `both.txt` is the path the owned file covers, so bundle re-derives it.
    assert routed["both.txt"] == "deferred_regen"

    ledger = conflict_set.ConflictSet.from_json(ledger_out.read_text(encoding="utf-8"))
    assert [entry.path for entry in ledger.entries()] == paths
    assert ledger.partition(Claimed.UNCLAIMED) == []
