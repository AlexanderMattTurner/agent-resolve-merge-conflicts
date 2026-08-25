"""Behavioral tests for prose-follows-code: a conflict block that is a docstring
paragraph is decided by the code block it argues for, not by a shard of its own.

The case: PR #4871 declined `tests/test_safe_launch_latency.py`, whose three blocks
sat inside one test. Two of them were opposed docstring paragraphs, each arguing for
its own side's body, so a shard reading the prose alone could only decline — and the
whole file kept its markers although the body was decidable.

Contract:
  * the prose block is launched at NO shard, so the run pays for the code only;
  * the prose takes the side its code block resolved to;
  * a code block that declined, or that blended both sides, leaves the prose its
    markers — a decline stays a decline, and the file still reaches the marker sweep.
"""

# covers: .github/resolver/auto-resolve/_prose_blocks.py

import json
from pathlib import Path

import pytest

from tests._resolver_helpers import load_script

# fanout first: loading the entry point is what puts its sibling modules on the
# import path, and `_prose_blocks` imports one of them.
fanout = load_script(".github/resolver/auto-resolve/fanout.py")
prose_blocks = load_script(".github/resolver/auto-resolve/_prose_blocks.py")

OURS_PARAGRAPH = "    A floor of twice the runner's own idle node start holds.\n"
THEIRS_PARAGRAPH = "    A sleeping hook clears a fixed 400 ms floor everywhere.\n"
OURS_BODY = "    assert report_for(scaled_floor()) is None\n"
THEIRS_BODY = "    assert report_for(sleeping_hook(400)) is None\n"


# OURS puts block 1 inside a docstring it opens and closes; THEIRS makes the same
# lines an assignment. The two whole-side views tokenize block 1 to different kinds.
SIDE_DISAGREEMENT = (
    "def test_one():\n"
    "<<<<<<< HEAD\n"
    '    """Why the floor is what it is.\n'
    "\n"
    f"{OURS_PARAGRAPH}"
    '    """\n'
    "=======\n"
    "    floor = sleeping_hook(400)\n"
    ">>>>>>> main\n"
    "<<<<<<< HEAD\n"
    f"{OURS_BODY}"
    "=======\n"
    f"{THEIRS_BODY}"
    ">>>>>>> main\n"
)


def _conflicted(
    ours_doc: str = OURS_PARAGRAPH,
    theirs_doc: str = THEIRS_PARAGRAPH,
    ours_body: str = OURS_BODY,
    theirs_body: str = THEIRS_BODY,
) -> str:
    """One test function with two conflict blocks: its docstring's argument, and
    the body that argument is about."""
    return (
        "def test_a_loaded_machine_never_produces_a_report():\n"
        '    """Why the floor is what it is.\n'
        "\n"
        "<<<<<<< HEAD\n"
        f"{ours_doc}"
        "=======\n"
        f"{theirs_doc}"
        ">>>>>>> main\n"
        '    """\n'
        "    load = hold_the_machine()\n"
        "<<<<<<< HEAD\n"
        f"{ours_body}"
        "=======\n"
        f"{theirs_body}"
        ">>>>>>> main\n"
    )


def test_the_docstring_block_is_paired_with_the_body_it_argues_for() -> None:
    assert prose_blocks.follower_pairs("t.py", _conflicted()) == {1: 2}


def test_two_code_blocks_stay_independent() -> None:
    """The pairing is not "the nearest block": two blocks that are both code are
    two decisions, and merging one says nothing about the other."""
    text = (
        "def test_one():\n"
        '    """A docstring both sides left alone."""\n'
        "<<<<<<< HEAD\n"
        "    load = hold_the_machine(2)\n"
        "=======\n"
        "    load = hold_the_machine(4)\n"
        ">>>>>>> main\n"
        "<<<<<<< HEAD\n"
        f"{OURS_BODY}"
        "=======\n"
        f"{THEIRS_BODY}"
        ">>>>>>> main\n"
    )
    assert prose_blocks.follower_pairs("t.py", text) == {}


def test_blocks_in_different_definitions_stay_independent() -> None:
    """A docstring argues for the body under IT. A body three functions away is a
    separate decision, and following it would take a side nobody argued for."""
    text = (
        "def one():\n"
        '    """Head.\n'
        "\n"
        "<<<<<<< HEAD\n"
        "    ours paragraph\n"
        "=======\n"
        "    theirs paragraph\n"
        ">>>>>>> main\n"
        '    """\n'
        "    return 1\n"
        "\n"
        "\n"
        "def two():\n"
        "<<<<<<< HEAD\n"
        "    return 2\n"
        "=======\n"
        "    return 3\n"
        ">>>>>>> main\n"
    )
    assert prose_blocks.follower_pairs("t.py", text) == {}


def test_two_code_blocks_in_one_definition_leave_the_docstring_alone() -> None:
    """Two conflicted statements can resolve to opposite sides, so neither is the
    winner the paragraph follows. Pairing with one of them would take the paragraph
    from OURS while half the body came from THEIRS."""
    text = (
        "def test_one():\n"
        '    """Why the floor is what it is.\n'
        "\n"
        "<<<<<<< HEAD\n"
        f"{OURS_PARAGRAPH}"
        "=======\n"
        f"{THEIRS_PARAGRAPH}"
        ">>>>>>> main\n"
        '    """\n'
        "<<<<<<< HEAD\n"
        "    load = hold_the_machine(2)\n"
        "=======\n"
        "    load = hold_the_machine(4)\n"
        ">>>>>>> main\n"
        "<<<<<<< HEAD\n"
        f"{OURS_BODY}"
        "=======\n"
        f"{THEIRS_BODY}"
        ">>>>>>> main\n"
    )
    assert prose_blocks.follower_pairs("t.py", text) == {}


def test_a_conflicted_comment_is_not_prose_that_follows_the_body() -> None:
    """A comment about logging and a return statement change independently, so the
    comment argues for nothing and keeps the shard it has today. Only a definition's
    docstring is prose the body decides."""
    text = (
        "def test_one():\n"
        '    """A docstring both sides left alone."""\n'
        "<<<<<<< HEAD\n"
        "    # log the scaled floor\n"
        "=======\n"
        "    # log the sleeping hook\n"
        ">>>>>>> main\n"
        "<<<<<<< HEAD\n"
        f"{OURS_BODY}"
        "=======\n"
        f"{THEIRS_BODY}"
        ">>>>>>> main\n"
    )
    assert prose_blocks.follower_pairs("t.py", text) == {}


def test_sides_that_read_the_block_differently_pair_nothing() -> None:
    """The agreement gate: OURS opens a docstring inside block 1, THEIRS makes the
    same lines a statement. OURS alone would pair the two blocks, and a pair resting
    on one side's reading is a pair the other side never agreed to."""
    assert prose_blocks.follower_pairs("t.py", SIDE_DISAGREEMENT) == {}


def test_the_two_side_views_of_that_file_really_do_disagree() -> None:
    """The fixture above proves nothing if both sides read block 1 alike: a pair
    would then be refused for some other reason and the gate stay uncovered."""
    blocks = prose_blocks.hunks_of(SIDE_DISAGREEMENT)
    reads = [
        prose_blocks._classify(prose_blocks._side_view(SIDE_DISAGREEMENT, side), blocks)
        for side in (prose_blocks.OURS, prose_blocks.THEIRS)
    ]
    assert reads[0][1] == (prose_blocks.PROSE, "test_one")
    assert reads[1][1] == (prose_blocks.CODE, "test_one")


def test_a_format_with_no_reader_here_is_left_alone() -> None:
    """Every non-Python path keeps today's behaviour: one shard per block."""
    assert prose_blocks.follower_pairs("t.mjs", _conflicted()) == {}


def test_a_file_that_does_not_parse_pairs_nothing() -> None:
    text = "def broken(:\n<<<<<<< HEAD\n    x\n=======\n    y\n>>>>>>> main\n"
    assert prose_blocks.follower_pairs("t.py", text) == {}


@pytest.mark.parametrize(
    ("resolved", "expected"),
    [(OURS_BODY, 0), (THEIRS_BODY, 1), ("    assert both()\n", None)],
)
def test_the_winning_side_is_the_one_the_resolution_equals(
    resolved: str, expected: int | None
) -> None:
    """A blend belongs to neither parent, so it names no side for the prose to
    follow — which is the answer that keeps the paragraph's markers."""
    block = f"<<<<<<< HEAD\n{OURS_BODY}=======\n{THEIRS_BODY}>>>>>>> main\n"
    assert prose_blocks.winning_side(block, resolved) == expected


def test_a_resolution_matching_both_sides_names_neither() -> None:
    """Two alternatives that differ only in whitespace both match the delivered
    text, so the fragment does not say which branch it came from. Answering OURS
    because it is checked first would install the PR-side paragraph on a coin
    toss."""
    block = f"<<<<<<< HEAD\n{OURS_BODY}=======\n{OURS_BODY.rstrip()}  \n>>>>>>> main\n"
    assert prose_blocks.winning_side(block, OURS_BODY) is None


# --------------------------------------------------------- through the fan-out


def _planned(tmp_path, monkeypatch, text: str, name: str = "t.py"):
    """A Fanout planned over one conflicted file in a scratch tree."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / name).write_text(text, encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    instance = fanout.Fanout()
    instance.files = [name]
    instance.dir = logs
    instance.pr_number = "123"
    instance.aggregate_file = logs / "execution.json"
    instance.verdict_file = logs / "modify-delete-verdicts.json"
    instance.resolution_file = logs / "sidecar-resolutions.json"
    instance.plan_work()
    return instance


def test_no_shard_is_launched_for_the_prose_block(tmp_path, monkeypatch) -> None:
    """The saving and the fix are the same change: the paragraph is decided from
    the body's resolution, so the run pays one model call instead of two."""
    instance = _planned(tmp_path, monkeypatch, _conflicted())
    assert [work.hunk.ordinal for work in instance.work] == [2]


def test_the_prose_takes_the_side_that_won_its_code_block(
    tmp_path, monkeypatch
) -> None:
    instance = _planned(tmp_path, monkeypatch, _conflicted())
    Path(instance.resolved_path(0)).write_text(THEIRS_BODY, encoding="utf-8")
    instance.install_resolutions()
    merged = (tmp_path / "t.py").read_text(encoding="utf-8")
    assert "<<<<<<<" not in merged
    assert THEIRS_PARAGRAPH in merged
    assert OURS_PARAGRAPH not in merged
    assert THEIRS_BODY in merged


def test_a_declined_code_block_takes_its_prose_with_it(
    tmp_path, monkeypatch, capsys
) -> None:
    """The decline still leaves markers and still reports: the paragraph cannot
    hold when the technique it argues for was never chosen."""
    instance = _planned(tmp_path, monkeypatch, _conflicted())
    before = (tmp_path / "t.py").read_text(encoding="utf-8")
    instance.install_resolutions()
    assert (tmp_path / "t.py").read_text(encoding="utf-8") == before
    assert "keeps its conflict markers" in capsys.readouterr().out


def test_a_blended_code_block_leaves_the_prose_its_markers(
    tmp_path, monkeypatch
) -> None:
    """A resolution that is neither side names no side, so the paragraph keeps its
    markers and the residue pass reads a file whose body is already settled."""
    instance = _planned(tmp_path, monkeypatch, _conflicted())
    Path(instance.resolved_path(0)).write_text(
        "    assert report_for(both()) is None\n", encoding="utf-8"
    )
    instance.install_resolutions()
    merged = (tmp_path / "t.py").read_text(encoding="utf-8")
    assert "both()" in merged
    assert merged.count("<<<<<<<") == 1, "the paragraph, and only the paragraph"


def test_the_shard_prompt_still_counts_every_block(tmp_path, monkeypatch) -> None:
    """The body's shard is told the file has two blocks and that one is its own.
    A count that dropped the prose block would describe a file the shard cannot
    see, and the shard reads the whole file for context."""
    instance = _planned(tmp_path, monkeypatch, _conflicted())
    prompt = instance.shard_prompt_for(0, instance.work[0])
    assert "The file has 2 conflict block" in prompt
    assert "block number 2" in prompt


def test_the_aggregate_names_only_the_shards_that_ran(tmp_path, monkeypatch) -> None:
    """The prose block has no shard, so nothing reports one: a summary for a run
    that never happened would read as a silent shard and fail the fan-out."""
    instance = _planned(tmp_path, monkeypatch, _conflicted())
    Path(instance.resolved_path(0)).write_text(THEIRS_BODY, encoding="utf-8")
    Path(f"{instance.dir}/0.exit").write_text("0\n", encoding="utf-8")
    summaries = [instance.shard_summary(0, instance.work[0])]
    instance.aggregate(summaries)
    written = json.loads(instance.aggregate_file.read_text(encoding="utf-8"))
    assert len(written["shards"]) == 1
