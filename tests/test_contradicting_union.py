"""The contradicting-union report, driven against hand-built parents and against
a real merge of two branches.

covers: .github/resolver/auto-resolve/_contradicting_union.py
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._resolver_helpers import load_script

union = load_script(".github/resolver/auto-resolve/_contradicting_union.py")
git_io = sys.modules["_git_io"]

# The pair agent-glovebox #5606 merged: the base branch asserted the entry was
# absent, the pull request asserted it was present, and the merge kept both.
OURS = '        assert f"Read({path})" in deny, deny'
THEIRS = '        assert f"Read({path})" not in deny, deny'
EDIT = '        assert f"Edit({path})" in deny, deny'


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("assert value in deny", "assert value not in deny"),
        ("assert reading is fresh", "assert reading is not fresh"),
        ("if (ok === want) {", "if (ok !== want) {"),
        ("while (ready) {", "while (!ready) {"),
    ],
    ids=["not-in", "is-not", "bang-equals", "bang"],
)
def test_a_line_and_its_negation_share_one_polarity_free_form(one, other):
    assert union.polarity_free(one) == union.polarity_free(other)


def test_two_different_statements_do_not_share_one_polarity_free_form():
    assert union.polarity_free("assert value in deny") != union.polarity_free(
        "assert other in deny"
    )


def test_a_merge_that_kept_both_parents_assertions_names_both_lines():
    """Each line traces to a parent, so nothing else in the resolver looks at
    this: no conflict, no neither-side line, and a delta review that reads a
    delta in which every line is somebody's."""
    merged = f"for path in x:\n{OURS}\n{EDIT}\n{THEIRS}\n"
    assert union.contradicting_line_numbers({OURS}, {THEIRS}, merged, ".py") == [2, 4]


def test_a_resolution_that_kept_one_side_is_reported_as_nothing():
    """Choosing is the answer this check wants, so it must stay silent for one."""
    merged = f"for path in x:\n{OURS}\n{EDIT}\n"
    assert union.contradicting_line_numbers({OURS}, {THEIRS}, merged, ".py") == []


@pytest.mark.parametrize(
    ("filler", "expected"),
    [
        (union._SEAM_LINES - 1, [1, union._SEAM_LINES + 1]),  # noqa: SLF001
        (union._SEAM_LINES, []),  # noqa: SLF001
    ],
    ids=["at-the-bound", "past-it"],
)
def test_distance_is_what_tells_one_seam_from_two_functions(filler, expected):
    """Two functions of one file legitimately assert a thing and its negation.
    A merge cannot create that pair out of adjacent text, so distance is what
    tells the two readings apart, and the bound itself still reads as one seam."""
    merged = f"{OURS}\n" + "filler\n" * filler + f"{THEIRS}\n"
    assert union.contradicting_line_numbers({OURS}, {THEIRS}, merged, ".py") == expected


def test_a_pair_that_differs_only_in_spacing_is_not_a_contradiction():
    """Whitespace flattening gives these one polarity-free form, but neither
    line negates the other, so reporting them would disable auto-merge over a
    re-spaced copy of one statement."""
    ours, theirs = "assert  value in deny", "assert value in deny"
    merged = f"{ours}\n{theirs}\n"
    assert union.contradicting_line_numbers({ours}, {theirs}, merged, ".py") == []


def test_a_short_executable_condition_is_still_a_contradiction():
    """`if x:` is five characters of executable syntax. A length floor drops it
    and the merged file keeps both readings of the same condition."""
    merged = "if x:\n    go()\nif not x:\n"
    assert union.contradicting_line_numbers(
        {"if x:"}, {"if not x:"}, merged, ".py"
    ) == [1, 3]


def test_a_block_both_parents_added_whole_is_not_a_merge_created_pair():
    """A cherry-pick lands the same block on both branches, and the block itself
    asserts a flag present and then absent. Each parent already carried the pair,
    so the merge of the two created nothing."""
    block = {OURS, THEIRS}
    merged = f"{OURS}\n{EDIT}\n{THEIRS}\n"
    assert union.contradicting_line_numbers(block, block, merged, ".py") == []


def test_a_merged_line_the_hooks_re_spaced_is_still_found():
    """The repo's hooks and the post-merge repair rewrite the tree before this
    report runs, so the merged text holds a reformatted copy of each parent's
    line rather than the bytes git recorded as added."""
    merged = f"for path in x:\n{OURS.replace('assert', 'assert ')}\n{THEIRS}\n"
    assert union.contradicting_line_numbers({OURS}, {THEIRS}, merged, ".py") == [2, 3]


def test_a_pair_at_different_indentation_is_not_one_seam():
    """Different indentation is a different block, and a block the other parent
    never wrote into."""
    merged = f"{OURS}\n{THEIRS.strip()}\n"
    assert (
        union.contradicting_line_numbers({OURS}, {THEIRS.strip()}, merged, ".py") == []
    )


def test_a_pair_of_contradicting_comments_is_not_reported():
    """Prose contradicts prose all the time, and no program reads it."""
    ours, theirs = "# the tier ships with it", "# the tier ships without it"
    merged = f"{ours}\n{theirs}\n"
    assert union.contradicting_line_numbers({ours}, {theirs}, merged, ".py") == []


def _git(work: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(work), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    ).stdout


BASE_BODY = f"for path in x:\n{EDIT}\n"


@pytest.fixture(name="merged")
def _merged(tmp_path, monkeypatch):
    """Two branches that each added one assertion to the same loop, with the
    worktree parked on their merge. Returns their two tips.

    A real repository, not a hand-built pair of line sets: the check reads what
    each parent ADDED out of git, and a diff parse that misfiled a path would
    pass every test above."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "commit.gpgsign", "false")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@e")
    (work / "deny.py").write_text(BASE_BODY, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "deny.py").write_text(f"for path in x:\n{OURS}\n{EDIT}\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "feature")
    _git(work, "checkout", "-q", "main")
    (work / "deny.py").write_text(
        f"for path in x:\n{EDIT}\n{THEIRS}\n", encoding="utf-8"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "main")
    monkeypatch.chdir(work)
    # Recorded BEFORE the bind, so the binding this fixture makes is undone: an
    # unbound `_git_io` is the safe state, and a later test that forgets to bind
    # must get its refusal rather than this fixture's throwaway repository.
    monkeypatch.setattr(git_io, "_REPO", None, raising=False)
    git_io.bind_repo(work)
    return (
        _git(work, "rev-parse", "feature").strip(),
        _git(work, "rev-parse", "main").strip(),
    )


def test_a_real_merge_that_kept_both_additions_is_reported(merged):
    """The whole defect, end to end: the worktree holds a loop that asserts an
    entry is present and absent, every line of it traces to a parent, and this
    is what names the two lines so `land` turns auto-merge off."""
    head, base = merged
    Path("deny.py").write_text(
        f"for path in x:\n{OURS}\n{EDIT}\n{THEIRS}\n", encoding="utf-8"
    )
    assert union.unions_that_contradict(head, base) == {"deny.py": "2, 4"}


def test_a_real_merge_that_chose_one_side_reports_nothing(merged):
    head, base = merged
    Path("deny.py").write_text(f"for path in x:\n{OURS}\n{EDIT}\n", encoding="utf-8")
    assert union.unions_that_contradict(head, base) == {}


def test_a_hunk_line_shaped_like_a_diff_header_is_read_as_an_addition(merged):
    """`+++ x` inside a hunk is a file whose own content holds `++ x`. Read as a
    header, every addition below it files under a path nothing in the tree
    carries, and this check then reports nothing on the real file."""
    base = merged[1]
    work = Path.cwd()
    (work / "notes.txt").write_text(f"++ x\n{OURS}\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "header-shaped")
    added = union.added_lines(base, _git(work, "rev-parse", "HEAD").strip())
    assert added["notes.txt"] == {"++ x", OURS}


def test_a_renamed_file_adds_only_what_the_parent_changed(tmp_path, monkeypatch):
    """Rename detection off reads the new name as a new file, so the body the
    rename carried over counts as added. A pair the ancestor already held then
    reads as one this merge created."""
    work = tmp_path / "renamed"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "commit.gpgsign", "false")
    body = f"for path in x:\n{OURS}\n{THEIRS}\n"
    (work / "deny.py").write_text(body, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    base = _git(work, "rev-parse", "HEAD").strip()
    _git(work, "mv", "deny.py", "checks.py")
    (work / "checks.py").write_text(f"{body}{EDIT}\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "rename")
    head = _git(work, "rev-parse", "HEAD").strip()
    monkeypatch.chdir(work)
    monkeypatch.setattr(git_io, "_REPO", None, raising=False)
    git_io.bind_repo(work)
    assert union.added_lines(base, head) == {"checks.py": {EDIT}}


def test_a_path_the_resolution_deleted_reports_nothing(merged):
    """A deleted path has no merged text to read, and raising there would refuse
    a resolution whose answer was to drop the file."""
    head, base = merged
    Path("deny.py").unlink()
    assert union.unions_that_contradict(head, base) == {}


# A `!` inside a shell command string, and a `not` after a comment marker: each
# reads as a negation to a text scan, and each then pairs the line that merely
# lacks it. Only masking a literal and a comment tells them from code.
@pytest.mark.parametrize(
    ("one", "other"),
    [
        ('    run("rm -rf !important")', '    run("rm -rf important")'),
        ('    msg = "not found"', '    msg = "found"'),
        ("    keep = ok  # not the negated one", "    keep = ok"),
    ],
    ids=["bang-in-a-string", "not-in-a-string", "not-in-a-comment"],
)
def test_a_negation_inside_a_literal_or_a_comment_pairs_nothing(one, other):
    merged = f"def f():\n{one}\n{other}\n"
    assert union.contradicting_line_numbers({one}, {other}, merged, ".py") == []


def test_a_trailing_comment_does_not_hide_a_real_contradiction():
    """The comment is dropped rather than masked, so the commented line still
    keys with the same statement written without one."""
    one, other = "    ok = a != b  # sign of life", "    ok = a == b"
    merged = f"def f():\n{one}\n{other}\n"
    assert union.contradicting_line_numbers({one}, {other}, merged, ".py") == [2, 3]


def test_a_language_with_no_tokenizer_here_judges_only_unquoted_lines():
    """A `.go` line with no quote is read; one carrying a quote is refused,
    because nothing here can tell that file's code from its text."""
    plain = ("\tok := a != b", "\tok := a == b")
    quoted = ('\trun("rm -rf !x")', '\trun("rm -rf x")')
    for pair, want in ((plain, [2, 3]), (quoted, [])):
        merged = f"func f() {{\n{pair[0]}\n{pair[1]}\n"
        assert (
            union.contradicting_line_numbers({pair[0]}, {pair[1]}, merged, ".go")
            == want
        )


def test_the_pair_that_motivated_this_check_still_reports():
    """agent-glovebox #5606's own lines, which carry an f-string: the mask must
    keep them keyed together, or this whole check reports nothing."""
    merged = f"for path in x:\n{OURS}\n{THEIRS}\n"
    assert union.contradicting_line_numbers({OURS}, {THEIRS}, merged, ".py") == [2, 3]
