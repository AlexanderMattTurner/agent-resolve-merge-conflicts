"""The formatter-padding pre-pass, driven against a REAL git merge conflict.

Each case builds a scratch repository holding a markdown table padded the way
prettier pads one — every cell to the width of the widest cell in its column —
and lets `git merge` produce the conflict. One added row with a longer cell
rewrites every row, so git hands the whole table over as a single hunk.

Two load-bearing assertions. BOTH sides' semantic edits survive a hunk the pass
resolves: a pass that merely took a side would drop the other's row and still
leave a marker-free file, so the marker check alone would not catch it. And
every byte OUTSIDE the conflict comes back as git wrote it, padding included:
the pass re-merges whole files, so a re-formatted table elsewhere in the
document is the way that goes wrong.
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

# A second padded table, which no side edits and no conflict covers.
_OTHER_ROWS = [
    ("Flag", "Meaning"),
    ("--dry-run", "print the plan and change nothing"),
    ("--force", "go anyway"),
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


def _neighboured(rows: list[tuple[str, str]]) -> str:
    """ROWS as a padded table, under another padded table and between paragraphs.

    Everything but ROWS is the same on all three sides, so the merge puts none
    of it in conflict and the pass must hand every byte of it back untouched.
    """
    return (
        f"# Configuration\n\n{_rows(_OTHER_ROWS)}\n"
        f"The paragraph between the tables.\n\n{_rows(rows)}\n"
        "The paragraph under them.\n"
    )


def _fenced(rows: list[tuple[str, str]]) -> str:
    """ROWS as a padded table WRITTEN OUT inside a fenced code block, where the
    spacing is the example's content and the fence sits outside the hunk."""
    return f"# Configuration\n\n```text\n{_rows(rows)}```\n"


def _sides(written) -> dict:
    """WRITTEN as a path -> text map, so a case may name a second file."""
    return {_DOC: written} if isinstance(written, str) else written


def _merge(repo, ref: str, *, diff3: bool = True) -> None:
    """Merge REF into the checked-out branch and stop on the conflict."""
    style = ["-c", "merge.conflictStyle=diff3"] if diff3 else []
    subprocess.run(
        ["git", *style, "merge", "--no-commit", ref],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        check=False,
    )


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
    _merge(repo, "base-side", diff3=diff3)
    return repo


def _log_row(rows: list[tuple[str, str]], effect: str) -> list[tuple[str, str]]:
    """ROWS with GB_LOG's effect cell rewritten to EFFECT."""
    return [row if row[0] != "GB_LOG" else ("GB_LOG", effect) for row in rows]


_SETTLED = _log_row(_BASE_ROWS, "log path settled here")


def _criss_cross_repo(tmp_path):
    """A mid-merge repo whose own merge base is a merge that conflicted.

    main and topic each rewrite GB_LOG, then each merges the other and settles
    that row the same way. Their two merge commits have TWO merge bases, so git
    builds a virtual ancestor by merging those — and that merge conflicts, so
    the ancestor git records in index stage 1 carries markers of its own. Then
    each branch makes the padding edit this pass exists for.
    """
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {_DOC: _table(_BASE_ROWS)}, "the table")
    git_out(repo, "checkout", "-q", "-b", "topic")
    commit_files(repo, {_DOC: _table(_log_row(_BASE_ROWS, "log path for topic"))}, "t")
    git_out(repo, "checkout", "-q", "main")
    commit_files(repo, {_DOC: _table(_log_row(_BASE_ROWS, "log path for main"))}, "m")
    early_main = git_out(repo, "rev-parse", "HEAD")
    for branch, ref in (("main", "topic"), ("topic", early_main)):
        git_out(repo, "checkout", "-q", branch)
        _merge(repo, ref)
        (repo / _DOC).write_text(_table(_SETTLED), encoding="utf-8")
        git_out(repo, "add", "--", _DOC)
        git_out(repo, "commit", "-q", "-m", "settle the log row")
    commit_files(repo, {_DOC: _table(_theirs_of(_SETTLED))}, "topic lengthens a cell")
    git_out(repo, "checkout", "-q", "main")
    commit_files(repo, {_DOC: _table(_ours_of(_SETTLED))}, "main adds a row")
    _merge(repo, "topic")
    return repo


def _run(repo, monkeypatch):
    monkeypatch.chdir(repo)
    narrow.bind_repo(repo)
    return narrow.narrow_conflicts(narrow.unmerged_markdown())


def _ours_of(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """ROWS plus a row whose name is longer than every existing one, so the
    formatter re-pads every row: the pull request side's edit."""
    return rows + [("GLOVEBOX_VM_BACKEND", "picks the VM backend")]


def _theirs_of(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """ROWS with one Effect cell lengthened, which re-pads the same rows from
    the other column: the base side's edit."""
    return [
        row if row[0] != "GB_HOME" else ("GB_HOME", "where the sandbox state lives now")
        for row in rows
    ]


_OURS = _ours_of(_BASE_ROWS)
_THEIRS = _theirs_of(_BASE_ROWS)


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
    theirs = _log_row(_BASE_ROWS, "the base branch's log sentence")
    ours = _ours_of(_log_row(_BASE_ROWS, "the pull request's log sentence"))
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


def test_a_criss_cross_history_narrows(tmp_path, monkeypatch):
    """Two merge bases, and the virtual ancestor between them conflicted.

    Git writes that ancestor's own markers into the base section, so peeling the
    three sides out of the marker block cannot recover the base git merged
    against. The index stages carry it whole, so this resolves like any other.
    """
    repo = _criss_cross_repo(tmp_path)
    before = (repo / _DOC).read_text(encoding="utf-8")
    assert before.count("<<<<<<<") == 2, "the premise: a nested ancestor conflict"
    assert "picks the sandbox mode" in narrow.hunks_of(before)[0].text

    narrowed, resolved = _run(repo, monkeypatch)

    text = (repo / _DOC).read_text(encoding="utf-8")
    assert narrowed == [_DOC] and resolved == []
    conflicted = "".join(hunk.text for hunk in narrow.hunks_of(text))
    assert "picks the sandbox mode" not in conflicted, "an untouched row is out of it"
    assert "picks the sandbox mode" in text, "and it is still in the file"
    assert "GLOVEBOX_VM_BACKEND" in text, "the pull request's added row survived"
    assert "where the sandbox state lives now" in text, "the base's edit survived"


def test_a_conflict_style_with_no_base_section_still_narrows(tmp_path, monkeypatch):
    """Git's default conflict style writes ours and theirs and no ancestor, so a
    re-merge off the markers would report every row as a change both sides made.
    The ancestor is in index stage 1 whatever style wrote the working tree."""
    repo = _conflicted_repo(tmp_path, _table(_OURS), _table(_THEIRS), diff3=False)
    assert b"|||||||" not in (repo / _DOC).read_bytes(), "the premise: no base section"

    narrowed, resolved = _run(repo, monkeypatch)

    text = (repo / _DOC).read_text(encoding="utf-8")
    assert narrowed == [_DOC] and resolved == [_DOC]
    assert "GLOVEBOX_VM_BACKEND" in text and "where the sandbox state lives now" in text


def test_every_byte_outside_the_conflict_is_the_byte_git_wrote(tmp_path, monkeypatch):
    """The pass re-merges WHOLE files with the padding out of every table in
    them, so a table the merge never put in conflict is where that leaks. Those
    lines must come back with the formatter's widths exactly as git wrote them."""
    repo = _conflicted_repo(
        tmp_path,
        _neighboured(_OURS),
        _neighboured(_THEIRS),
        tracked={_DOC: _neighboured(_BASE_ROWS)},
    )
    before = (repo / _DOC).read_text(encoding="utf-8").splitlines(keepends=True)
    spans = narrow.hunk_line_ranges("".join(before))
    assert len(spans) == 1
    first, last = spans[0]

    narrowed, resolved = _run(repo, monkeypatch)

    after = (repo / _DOC).read_text(encoding="utf-8").splitlines(keepends=True)
    assert narrowed == [_DOC] and resolved == [_DOC]
    assert "| --dry-run | print the plan and change nothing |\n" in before[: first - 1]
    assert after[: first - 1] == before[: first - 1], "the untouched table kept its pad"
    assert after[len(after) - (len(before) - last) :] == before[last:]


def test_prose_beside_the_table_is_left_exactly_as_git_wrote_it(tmp_path, monkeypatch):
    """The two sides disagree about a paragraph, not about a table, so there is
    no padding to take out and the re-merge reproduces git's own conflict."""
    prose_ours = _table(_BASE_ROWS) + "\nThe pull request's paragraph.\n"
    prose_theirs = _table(_BASE_ROWS) + "\nThe base branch's paragraph.\n"
    repo = _conflicted_repo(tmp_path, prose_ours, prose_theirs)
    before = (repo / _DOC).read_bytes()

    assert _run(repo, monkeypatch) == ([], [])
    assert (repo / _DOC).read_bytes() == before


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("|  GB_MODE   |  picks it   |\n", "| GB_MODE | picks it |\n"),
        ("| :-------: | ----------: |\n", "| :-: | --: |\n"),
        (r"| a \| b | c |", r"| a \| b | c |"),
        ("| GB_HOME | path | - |\n", "| GB_HOME | path | - |\n"),
        ("| GB_HOME | path | ----- |\n", "| GB_HOME | path | ----- |\n"),
        ("  |  GB_MODE  |  -  |\n", "  | GB_MODE | - |\n"),
        ("|  GB_MODE  |  picks it  |\r\n", "| GB_MODE | picks it |\r\n"),
    ],
)
def test_a_row_normalizes_to_one_space_a_side(line, want):
    """The padding comes out and nothing else does. An escaped pipe is content
    inside a cell, so splitting on it would tear the cell in two. A cell of
    hyphens is content too wherever the row is not ALL delimiter cells — `-` is
    how a table says "no default", and `-` against `---` on the other side would
    otherwise merge clean with one side's value gone. The indentation stays: a
    table nested under a list item is indented, and moving it changes what the
    markdown says. A CRLF row keeps its CRLF."""
    assert narrow.normalize_row(line) == want


@pytest.mark.parametrize(
    "row",
    [
        # A row whose closing `|` is escaped: the last cell sits AFTER the final
        # separator, so normalizing it would drop that cell's content.
        "| c | d \\|\n",
        # Four spaces of indentation open a code block, where the spacing is
        # content rather than a formatter's padding.
        "    |  c  |  d  |\n",
    ],
)
def test_a_row_this_pass_cannot_rewrite_losslessly_is_left_alone(row):
    """The rows above it still normalize; this one comes back byte for byte."""
    document = f"|  a  |  b  |\n| ---- | ---- |\n{row}"

    answer = narrow.normalize_document(document)

    assert answer.startswith("| a | b |\n| --- | --- |\n")
    assert answer.endswith(row)


def test_a_line_that_only_looks_like_a_row_is_left_alone():
    """A table opens on a row followed by a DELIMITER row. Without that second
    line the leading `|` is a paragraph's own character, and its spacing is
    content nobody derived from a column width."""
    document = "A sentence.\n|  not a table  |  really  |\n\nAnd another.\n"

    assert narrow.normalize_document(document) == document


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


def test_a_table_written_out_inside_a_code_fence_is_left_alone(tmp_path, monkeypatch):
    """The fence tells a table from an example of one, whose every space is
    content. It is read from the whole document, which the index stages are."""
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
    """The file is rewritten whole, so reading it — or the index stages it is
    re-merged from — with the default newline translation would turn every CRLF
    into a bare LF, a change to every line of the file the merge never asked
    for."""
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


def test_a_path_the_merge_left_without_three_stages_is_skipped(tmp_path, monkeypatch):
    """An add/add conflict records no merge base, so there is no ancestor to
    re-merge against and this pass has nothing to say about the path."""
    added = "docs/added.md"
    repo = _conflicted_repo(
        tmp_path,
        {_DOC: _table(_BASE_ROWS), added: _table(_OURS)},
        {_DOC: _table(_BASE_ROWS), added: _table(_THEIRS)},
    )
    before = (repo / added).read_bytes()
    monkeypatch.chdir(repo)
    narrow.bind_repo(repo)
    unmerged = git_out(repo, "ls-files", "-u", "--", added).splitlines()
    assert {line.split()[2] for line in unmerged} == {"2", "3"}, (
        "the premise: git recorded ours and theirs for this path and no base"
    )

    assert narrow.narrow_conflicts([added]) == ([], [])
    assert (repo / added).read_bytes() == before


def _without(rows: list[tuple[str, str]], name: str) -> list[tuple[str, str]]:
    """ROWS with NAME's row dropped, which is what re-pads every remaining column."""
    return [row for row in rows if row[0] != name]


def test_one_side_deletes_a_row_while_the_other_edits_a_different_one(tmp_path):
    """Both intents survive: the deletion is honoured and the edit is kept.

    Stripping the padding is not enough on its own here. `git merge-file` is still line-based,
    so an edit to row N and a deletion of row N+1 are adjacent lines it calls one overlapping
    change — which is why agent-glovebox#5760 and #5890 reached a human with the whole table
    in front of them. A row is identified by its first cell, not its position.
    """
    repo = _conflicted_repo(
        tmp_path,
        ours=_table(_without(_BASE_ROWS, "GB_HOME")),
        theirs=_table(_log_row(_BASE_ROWS, "where the log goes, and it rotates daily")),
    )
    narrow.bind_repo(repo)
    _, resolved = narrow.narrow_conflicts([_DOC])
    assert resolved == [_DOC]
    merged = (repo / _DOC).read_text(encoding="utf-8")
    assert "GB_HOME" not in merged, merged
    assert "rotates daily" in merged, merged
    # The rows nobody touched are still there, so this is a merge and not a take-one-side.
    assert "GB_MODE" in merged, merged


def test_two_sides_editing_the_SAME_row_still_reach_a_human(tmp_path):
    """The row merge decides rows, never disagreements about one row.

    Without this the pass would invent an answer for the case it exists to route to a person.
    """
    repo = _conflicted_repo(
        tmp_path,
        ours=_table(_log_row(_BASE_ROWS, "the pull request's wording")),
        theirs=_table(_log_row(_BASE_ROWS, "the base branch's wording")),
    )
    narrow.bind_repo(repo)
    _, resolved = narrow.narrow_conflicts([_DOC])
    assert resolved == []
    assert "<<<<<<<" in (repo / _DOC).read_text(encoding="utf-8")


def test_a_row_one_side_edits_and_the_other_deletes_still_reaches_a_human(tmp_path):
    """An edit against a deletion of the SAME row is a judgement, not a row-level merge."""
    repo = _conflicted_repo(
        tmp_path,
        ours=_table(_without(_BASE_ROWS, "GB_LOG")),
        theirs=_table(_log_row(_BASE_ROWS, "the base branch keeps and rewords it")),
    )
    narrow.bind_repo(repo)
    _, resolved = narrow.narrow_conflicts([_DOC])
    assert resolved == []
    assert "<<<<<<<" in (repo / _DOC).read_text(encoding="utf-8")


def test_a_row_delete_and_a_row_edit_merge_around_a_second_table(tmp_path):
    """The same decision in a document holding TWO tables and prose between them.

    A configuration page usually has a table per section, so refusing those would leave the
    commonest real file on the line merge this pass replaces. Every byte outside the edited
    table must come back as git wrote it, padding included.
    """
    repo = _conflicted_repo(
        tmp_path,
        ours=_neighboured(_without(_BASE_ROWS, "GB_HOME")),
        theirs=_neighboured(_log_row(_BASE_ROWS, "where the log goes, and it rotates daily")),
        tracked={_DOC: _neighboured(_BASE_ROWS)},
    )
    narrow.bind_repo(repo)
    _, resolved = narrow.narrow_conflicts([_DOC])
    assert resolved == [_DOC]
    merged = (repo / _DOC).read_text(encoding="utf-8")
    assert "GB_HOME" not in merged, merged
    assert "rotates daily" in merged, merged
    # The untouched neighbour keeps its own padding, so the pass did not reformat the document.
    assert _rows(_OTHER_ROWS) in merged, merged
    assert "The paragraph between the tables." in merged, merged
