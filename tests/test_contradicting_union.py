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
    assert union.contradicting_line_numbers({OURS}, {THEIRS}, merged) == [2, 4]


def test_a_resolution_that_kept_one_side_is_reported_as_nothing():
    """Choosing is the answer this check wants, so it must stay silent for one."""
    merged = f"for path in x:\n{OURS}\n{EDIT}\n"
    assert union.contradicting_line_numbers({OURS}, {THEIRS}, merged) == []


def test_the_same_statement_and_its_negation_far_apart_are_not_one_seam():
    """Two functions of one file legitimately assert a thing and its negation.
    A merge cannot create that pair out of adjacent text, so distance is what
    tells the two readings apart."""
    merged = f"{OURS}\n" + "filler\n" * union._SEAM_LINES + f"{THEIRS}\n"  # noqa: SLF001
    assert union.contradicting_line_numbers({OURS}, {THEIRS}, merged) == []


def test_a_pair_at_different_indentation_is_not_one_seam():
    """Different indentation is a different block, and a block the other parent
    never wrote into."""
    merged = f"{OURS}\n{THEIRS.strip()}\n"
    assert union.contradicting_line_numbers({OURS}, {THEIRS.strip()}, merged) == []


def test_a_pair_of_contradicting_comments_is_not_reported():
    """Prose contradicts prose all the time, and no program reads it."""
    ours, theirs = "# the tier ships with it", "# the tier ships without it"
    merged = f"{ours}\n{theirs}\n"
    assert union.contradicting_line_numbers({ours}, {theirs}, merged) == []


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


def test_a_path_the_resolution_deleted_reports_nothing(merged):
    """A deleted path has no merged text to read, and raising there would refuse
    a resolution whose answer was to drop the file."""
    head, base = merged
    Path("deny.py").unlink()
    assert union.unions_that_contradict(head, base) == {}
