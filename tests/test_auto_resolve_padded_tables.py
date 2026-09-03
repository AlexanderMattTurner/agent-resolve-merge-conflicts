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


def _rows(rows: list[tuple[str, str]]) -> str:
    """ROWS as a table padded the way prettier pads one: every cell out to the
    width of the widest cell in its column, with a delimiter row to match."""
    widths = [max(len(row[column]) for row in rows) for column in (0, 1)]
    out = [
        f"| {rows[0][0].ljust(widths[0])} | {rows[0][1].ljust(widths[1])} |\n",
        f"| {'-' * widths[0]} | {'-' * widths[1]} |\n",
    ]
    out += [
        f"| {name.ljust(widths[0])} | {effect.ljust(widths[1])} |\n"
        for name, effect in rows[1:]
    ]
    return "".join(out)


def _table(rows: list[tuple[str, str]]) -> str:
    """ROWS as a padded table under a heading, which is one whole document."""
    return f"# Configuration\n\n{_rows(rows)}"


def _fenced(rows: list[tuple[str, str]]) -> str:
    """ROWS as a padded table WRITTEN OUT inside a fenced code block, where the
    spacing is the example's content and the fence sits outside the hunk."""
    return f"# Configuration\n\n```text\n{_rows(rows)}```\n"


def _sides(written) -> dict:
    """WRITTEN as a path -> text map, so a case may name a second file."""
    return {_DOC: written} if isinstance(written, str) else written


def _conflicted_repo(tmp_path, ours, theirs, *, diff3: bool = True, tracked=None):
    """A mid-merge repo whose two branches wrote OURS and THEIRS over the table.

    Each side is one document for `_DOC`, or a path -> text map when the case
    needs a second file. TRACKED joins the first commit. `diff3=False` runs the
    merge under git's default conflict style, which writes no base section.
    """
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_DOC: _table(_BASE_ROWS), **(tracked or {})}, "the table")
    git_out(repo, "checkout", "-q", "-b", "base-side")
    commit_files(repo, _sides(theirs), "the base branch edits the table")
    git_out(repo, "checkout", "-q", "main")
    commit_files(repo, _sides(ours), "the pull request edits the table")
    style = ["-c", "merge.conflictStyle=diff3"] if diff3 else []
    subprocess.run(
        ["git", *style, "merge", "--no-commit", "base-side"],
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
        ("| GB_HOME | path | - |\n", "| GB_HOME | path | - |\n"),
        ("| GB_HOME | path | ----- |\n", "| GB_HOME | path | ----- |\n"),
        ("  |  GB_MODE  |  -  |\n", "  | GB_MODE | - |\n"),
    ],
)
def test_a_row_normalizes_to_one_space_a_side(line, want):
    """The padding comes out and nothing else does. An escaped pipe is content
    inside a cell, so splitting on it would tear the cell in two. A cell of
    hyphens is content too wherever the row is not ALL delimiter cells — `-` is
    how a table says "no default", and `-` against `---` on the other side would
    otherwise merge clean with one side's value gone. The indentation stays: a
    table nested under a list item is indented, and moving it changes what the
    markdown says."""
    assert narrow.normalize_row(line) == want


@pytest.mark.parametrize(
    "text",
    [
        # A row whose closing `|` is escaped: the last cell sits AFTER the final
        # separator, so normalizing it would drop that cell's content.
        "| a | b \\|\n",
        # Four spaces of indentation open a code block, where the spacing is
        # content rather than a formatter's padding.
        "    | a | b |\n",
    ],
)
def test_a_line_this_pass_cannot_rewrite_losslessly_stops_the_side(text):
    """`normalize_side` answers None, so the whole hunk is left as git wrote it."""
    assert narrow.normalize_side(text) is None


def test_a_conflicted_non_markdown_path_is_none_of_this_passs_business(
    tmp_path, monkeypatch
):
    """A `|`-leading line is a table row only in markdown. Elsewhere it is
    content whose spacing this pass has no argument about."""
    other = "docs/table.txt"
    repo = _conflicted_repo(
        tmp_path,
        {_DOC: _table(_OURS), other: _table(_OURS)},
        {_DOC: _table(_THEIRS), other: _table(_THEIRS)},
        tracked={other: _table(_BASE_ROWS)},
    )
    before = (repo / other).read_bytes()

    monkeypatch.chdir(repo)
    narrow.bind_repo(repo)
    assert narrow.unmerged_markdown() == [_DOC]
    narrow.narrow_conflicts(narrow.unmerged_markdown())

    assert (repo / other).read_bytes() == before


def test_a_two_sided_conflict_is_left_whole(tmp_path, monkeypatch):
    """Without a base section there is no ancestor to merge against, so a
    re-merge would report every row as a change both sides made."""
    repo = _conflicted_repo(tmp_path, _table(_OURS), _table(_THEIRS), diff3=False)
    before = (repo / _DOC).read_bytes()
    assert b"|||||||" not in before, "the premise: git wrote no base section"

    assert _run(repo, monkeypatch) == ([], [])
    assert (repo / _DOC).read_bytes() == before


def test_a_table_written_out_inside_a_code_fence_is_left_alone(tmp_path, monkeypatch):
    """The fence sits outside the hunk, so row shape alone cannot tell a table
    from an example of one, whose every space is content."""
    repo = _conflicted_repo(
        tmp_path,
        _fenced(_OURS),
        _fenced(_THEIRS),
        tracked={_DOC: _fenced(_BASE_ROWS)},
    )
    before = (repo / _DOC).read_bytes()
    assert b"```text" in before

    assert _run(repo, monkeypatch) == ([], [])
    assert (repo / _DOC).read_bytes() == before


def test_a_path_whose_merge_the_repository_configured_is_left_to_that_driver(
    tmp_path, monkeypatch
):
    """`git merge-file` dispatches on no attribute and no driver, so merging such
    a path here would apply the policy the attribute exists to prevent."""
    repo = _conflicted_repo(
        tmp_path,
        _table(_OURS),
        _table(_THEIRS),
        tracked={".gitattributes": "*.md merge=mergiraf\n"},
    )
    before = (repo / _DOC).read_bytes()

    monkeypatch.chdir(repo)
    narrow.bind_repo(repo)
    assert narrow.unmerged_markdown() == []
    assert (repo / _DOC).read_bytes() == before


def test_a_path_another_pass_deferred_is_left_for_that_pass(tmp_path, monkeypatch):
    """prepare.sh names its deferred generated regions in this file. A text merge
    of a derived region is not the answer, and staging one would take the path
    out of the conflict list before its generator ever runs."""
    repo = _conflicted_repo(tmp_path, _table(_OURS), _table(_THEIRS))
    deferred = tmp_path / "deferred"
    deferred.write_text(f"{_DOC}\n", encoding="utf-8")
    monkeypatch.setenv(narrow.SKIP_FILE_ENV, str(deferred))
    before = (repo / _DOC).read_bytes()

    assert _run(repo, monkeypatch) == ([], [])
    assert (repo / _DOC).read_bytes() == before


def test_line_endings_outside_the_hunk_survive_the_rewrite(tmp_path, monkeypatch):
    """The file is rewritten whole, so reading it with the default newline
    translation would turn every CRLF in it into a bare LF — a change to every
    line of the file, none of which the merge asked for."""
    prose = "The paragraph above the table.\r\n\r\n"
    repo = _conflicted_repo(tmp_path, prose + _table(_OURS), prose + _table(_THEIRS))

    narrowed, resolved = _run(repo, monkeypatch)

    assert narrowed == [_DOC] and resolved == [_DOC]
    assert b"The paragraph above the table.\r\n" in (repo / _DOC).read_bytes()


def test_a_markdown_conflict_it_cannot_read_says_which_one(
    tmp_path, monkeypatch, capsys
):
    """The recovery is right — one undecodable file must not kill the pass for
    the others — but a silent skip reads in the log like a file with no work."""
    repo = _conflicted_repo(tmp_path, _table(_OURS), _table(_THEIRS))
    (repo / _DOC).write_bytes(b"caf\xe9\n")
    monkeypatch.chdir(repo)
    narrow.bind_repo(repo)

    assert narrow.narrow_conflicts([_DOC]) == ([], [])
    assert f"::warning::narrow-padded-tables: could not read {_DOC}" in (
        capsys.readouterr().err
    )
