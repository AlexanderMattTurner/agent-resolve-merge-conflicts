"""The formatter-padding pre-pass, driven against a REAL git merge conflict.

Each case builds a scratch repository holding a markdown table padded the way
prettier pads one — every cell to the width of the widest cell in its column —
and lets `git merge` produce the conflict. One added row with a longer cell
rewrites every row, so git hands the whole table over as a single hunk.

The load-bearing assertion is that BOTH sides' semantic edits survive a hunk the
pass resolves. A pass that merely took a side would drop the other's row and
still leave a marker-free file, so the marker check alone would not catch it.
"""

# covers: .github/resolver/auto-resolve/narrow_padded_tables.py

import subprocess

import pytest

from tests._helpers import commit_files, git_env, git_out, init_test_repo
from tests._resolver_helpers import load_script

narrow = load_script(".github/resolver/auto-resolve/narrow_padded_tables.py")

_DOC = "docs/configuration.md"

_BASE_ROWS = [
    ("Variable", "Effect"),
    ("GB_MODE", "picks the sandbox mode"),
    ("GB_HOME", "where the state lives"),
    ("GB_LOG", "where the log goes"),
]


def _table(rows: list[tuple[str, str]]) -> str:
    """ROWS as a table padded the way prettier pads one: every cell out to the
    width of the widest cell in its column, with a delimiter row to match."""
    widths = [max(len(row[column]) for row in rows) for column in (0, 1)]
    out = [
        "# Configuration\n",
        "\n",
        f"| {rows[0][0].ljust(widths[0])} | {rows[0][1].ljust(widths[1])} |\n",
        f"| {'-' * widths[0]} | {'-' * widths[1]} |\n",
    ]
    out += [
        f"| {name.ljust(widths[0])} | {effect.ljust(widths[1])} |\n"
        for name, effect in rows[1:]
    ]
    return "".join(out)


def _conflicted_repo(tmp_path, ours: str, theirs: str):
    """A mid-merge repo whose two branches wrote OURS and THEIRS over the table."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_DOC: _table(_BASE_ROWS)}, "the table")
    git_out(repo, "checkout", "-q", "-b", "base-side")
    commit_files(repo, {_DOC: theirs}, "the base branch edits the table")
    git_out(repo, "checkout", "-q", "main")
    commit_files(repo, {_DOC: ours}, "the pull request edits the table")
    subprocess.run(
        ["git", "-c", "merge.conflictStyle=diff3", "merge", "--no-commit", "base-side"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        check=False,
    )
    return repo


def _run(repo, monkeypatch):
    monkeypatch.chdir(repo)
    narrow.bind_repo(repo)
    return narrow.narrow_conflicts(narrow.unmerged_markdown())


# One added row whose name is longer than every existing one, so the formatter
# re-pads all four rows: the PR side's edit. The base side lengthens one Effect
# cell instead, which re-pads the same four rows from the other column.
_OURS = _BASE_ROWS + [("GLOVEBOX_VM_BACKEND", "picks the VM backend")]
_THEIRS = [
    row if row[0] != "GB_HOME" else ("GB_HOME", "where the sandbox state lives now")
    for row in _BASE_ROWS
]


def test_the_whole_table_conflicts_before_the_pass_runs(tmp_path):
    """The premise: git really does hand over every row, not the two that changed.

    Without this the cases below could pass against a conflict that was already
    narrow, and the pass would be measured on a problem it does not have.
    """
    repo = _conflicted_repo(tmp_path, _table(_OURS), _table(_THEIRS))
    text = (repo / _DOC).read_text(encoding="utf-8")

    hunks = narrow.hunks_of(text)
    assert len(hunks) == 1
    for name, _ in _BASE_ROWS:
        assert name in hunks[0].text, f"{name} was rewritten by the padding alone"


def test_padding_only_disagreement_resolves_and_keeps_both_edits(tmp_path, monkeypatch):
    """The defect this pass exists for: the two sides changed different rows, so
    stripping the derived widths leaves a merge with nothing to decide."""
    repo = _conflicted_repo(tmp_path, _table(_OURS), _table(_THEIRS))

    narrowed, resolved = _run(repo, monkeypatch)

    text = (repo / _DOC).read_text(encoding="utf-8")
    assert narrowed == [_DOC] and resolved == [_DOC]
    assert "<<<<<<<" not in text
    assert "GLOVEBOX_VM_BACKEND" in text, "the pull request's added row survived"
    assert "where the sandbox state lives now" in text, "the base's edit survived"
    assert "picks the sandbox mode" in text, "an untouched row is still there"
    assert git_out(repo, "diff", "--name-only", "--diff-filter=U") == "", (
        "a marker-free file must leave the unmerged set, or it reads downstream "
        "like a modify/delete conflict"
    )


def test_a_real_disagreement_keeps_markers_around_that_row_alone(tmp_path, monkeypatch):
    """Both sides rewrote the same cell, so the pass must not invent an answer —
    but the conflict it leaves covers that row, not the rows the padding moved."""
    theirs = [
        row if row[0] != "GB_LOG" else ("GB_LOG", "the base branch's log sentence")
        for row in _BASE_ROWS
    ]
    ours = [
        row if row[0] != "GB_LOG" else ("GB_LOG", "the pull request's log sentence")
        for row in _BASE_ROWS
    ] + [("GLOVEBOX_VM_BACKEND", "picks the VM backend")]
    repo = _conflicted_repo(tmp_path, _table(ours), _table(theirs))

    narrowed, resolved = _run(repo, monkeypatch)

    text = (repo / _DOC).read_text(encoding="utf-8")
    assert narrowed == [_DOC] and resolved == []
    conflicted = "".join(hunk.text for hunk in narrow.hunks_of(text))
    assert "the base branch's log sentence" in conflicted
    assert "the pull request's log sentence" in conflicted
    assert "GB_MODE" not in conflicted, "a row neither side touched is out of it"
    assert "GB_MODE" in text, "and it is still in the file"
    assert git_out(repo, "diff", "--name-only", "--diff-filter=U") == _DOC


def test_prose_beside_the_table_is_left_exactly_as_git_wrote_it(tmp_path, monkeypatch):
    """A leading `|` is a table row only in a table. A hunk holding anything else
    is one this pass has no argument about, so it must not be rewritten."""
    prose_ours = _table(_BASE_ROWS) + "\nThe pull request's paragraph.\n"
    prose_theirs = _table(_BASE_ROWS) + "\nThe base branch's paragraph.\n"
    repo = _conflicted_repo(tmp_path, prose_ours, prose_theirs)
    before = (repo / _DOC).read_text(encoding="utf-8")

    monkeypatch.chdir(repo)
    narrow.bind_repo(repo)
    assert narrow.narrow_text(before) is None
    assert (repo / _DOC).read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("|  GB_MODE   |  picks it   |\n", "| GB_MODE | picks it |\n"),
        ("| :-------: | ----------: |\n", "| :-: | --: |\n"),
        (r"| a \| b | c |", r"| a \| b | c |"),
    ],
)
def test_a_row_normalizes_to_one_space_a_side(line, want):
    """The padding comes out and nothing else does — an escaped pipe is content
    inside a cell, so splitting on it would tear the cell in two."""
    assert narrow.normalize_row(line) == want
