"""The checks that read a merge whose every line traces to a parent.

covers: .github/resolver/auto-resolve/_contradictory_merge.py

Each check is a heuristic, so its FILTERS are the contract — a case below that
stops firing is a false positive shipped to every resolution. The three motivating
merges are agent-glovebox #5568 (a split rename), #5641 (a revert undone) and
#5606 (a statement beside its negation).
"""

import subprocess
import sys

import pytest

from tests._resolver_helpers import load_script

contradictory_merge = load_script(
    ".github/resolver/auto-resolve/_contradictory_merge.py"
)
# The instance the module above imported, so binding it binds the one its git
# calls read. Loading `_git_io.py` again would make a second, unbound copy.
git_io = sys.modules["_git_io"]
added_lines = contradictory_merge.added_lines
orphaned_added_names = contradictory_merge.orphaned_added_names
resurrected_line_numbers = contradictory_merge.resurrected_line_numbers
contradicting_line_numbers = contradictory_merge.contradicting_line_numbers
describe_names = contradictory_merge.describe_names

_BASE = "import time\n\n\ndef wait(s):\n    time.sleep(s)\n"
_ALIASED = "import time\n\n_sleep = time.sleep\n\n\ndef wait(s):\n    _sleep(s)\n"
# What the merge produced: the alias from one parent, the plain call from the
# other. Every line traces to a parent and `_sleep` has no reader.
_SPLIT = "import time\n\n_sleep = time.sleep\n\n\ndef wait(s):\n    time.sleep(s)\n"
# A parent that bound a name and never read it. Same shape as _SPLIT, and the
# merge is innocent of it.
_DEAD_ON_ARRIVAL = (
    "import time\n\n_unused = time.sleep\n\n\ndef wait(s):\n    time.sleep(s)\n"
)


@pytest.mark.parametrize(
    ("base", "sides", "merged", "want"),
    [
        pytest.param(
            _BASE, [_ALIASED, _BASE], _SPLIT, ["_sleep"], id="the-split-rename"
        ),
        pytest.param(_BASE, [_ALIASED, _BASE], _ALIASED, [], id="one-whole-side-taken"),
        pytest.param(_BASE, [_ALIASED, _BASE], _BASE, [], id="the-other-whole-side"),
        # The parent never read the name it added, so it arrived dead and the
        # merge did not kill it. Blaming the resolution here blames it for the
        # branch. The merged text KEEPS the name, so only that clause declines.
        pytest.param(
            _BASE,
            [_DEAD_ON_ARRIVAL, _BASE],
            _DEAD_ON_ARRIVAL,
            [],
            id="a-name-its-own-parent-never-read",
        ),
        # A binding the base already carried is not this merge's doing.
        pytest.param(_SPLIT, [_ALIASED, _SPLIT], _SPLIT, [], id="already-in-the-base"),
        # `monkeypatch.setattr(mod, "_sleep", ...)` reaches the binding by its
        # spelling, so a name a fixture patches has a reader.
        pytest.param(
            _BASE,
            [_ALIASED, _BASE],
            _SPLIT + '\n\nfixture = ("_sleep",)\n',
            [],
            id="a-string-reaches-the-name",
        ),
        # Still carrying conflict markers. A half-parsed comparison would
        # misattribute what it finds, so this declines rather than raising.
        pytest.param(
            _BASE, ["<<<<<<< HEAD\n", _BASE], _SPLIT, [], id="an-unparseable-side"
        ),
        pytest.param(
            _BASE, [_ALIASED, _BASE], "def (\n", [], id="an-unparseable-merge"
        ),
        # A file that decodes as UTF-8 and holds a NUL byte reaches `ast.parse`,
        # which refuses it. This check must never be what kills a resolution, so
        # the refusal has to arrive as a declined read rather than as a raise.
        pytest.param(
            _BASE, [_ALIASED, _BASE], "x = 1\x00\n", [], id="a-merge-holding-a-NUL"
        ),
    ],
)
def test_a_binding_the_merge_left_with_no_reader(base, sides, merged, want):
    assert orphaned_added_names(base, sides, merged) == want


_LONG = "assert path in the_deny_list\n"


@pytest.mark.parametrize(
    ("base", "sides", "merged", "want"),
    [
        # Each parent's own commit deleted it, and the merge put it back.
        pytest.param(
            _LONG, ["x = 1\n", "y = 2\n"], "x = 1\n" + _LONG, [2], id="resurrected"
        ),
        # One parent still carries it, so the line traces to that parent.
        pytest.param(_LONG, [_LONG, "y = 2\n"], _LONG, [], id="a-parent-still-has-it"),
        # The base repeats it, so which copy came back says nothing.
        pytest.param(
            _LONG * 2, ["x = 1\n", "y = 2\n"], _LONG, [], id="the-base-repeated-it"
        ),
        # Below the length floor a merge legitimately repeats the line.
        pytest.param(
            "return\n", ["x = 1\n", "y = 2\n"], "return\n", [], id="too-short"
        ),
        pytest.param(
            "# " + "a comment that is long enough\n",
            ["x = 1\n", "y = 2\n"],
            "# a comment that is long enough\n",
            [],
            id="a-comment",
        ),
        # Long enough to clear the floor, so only the no-word filter can decline
        # it. A merge legitimately repeats a run of closing brackets.
        pytest.param(
            "]]]]]]]]]]]]\n",
            ["x = 1\n", "y = 2\n"],
            "]]]]]]]]]]]]\n",
            [],
            id="no-word",
        ),
    ],
)
def test_a_line_the_merge_brought_back(base, sides, merged, want):
    assert resurrected_line_numbers(base, sides, merged) == want


_IN = '    assert f"Read({path})" in deny, deny'
_NOT_IN = '    assert f"Read({path})" not in deny, deny'


@pytest.mark.parametrize(
    ("head_added", "base_added", "merged", "want"),
    [
        pytest.param(
            {_IN}, {_NOT_IN}, f"def f():\n{_IN}\n{_NOT_IN}\n", [2, 3], id="the-pair"
        ),
        # The resolution CHOSE, which is the answer this check wants.
        pytest.param({_IN}, {_NOT_IN}, f"def f():\n{_IN}\n", [], id="one-side-chosen"),
        # Further apart than one seam: two functions of one file legitimately
        # assert a thing and its negation.
        pytest.param(
            {_IN},
            {_NOT_IN},
            "def f():\n" + _IN + "\n" + "    pass\n" * 12 + _NOT_IN + "\n",
            [],
            id="past-the-seam",
        ),
        # Same statement twice, differing only in spacing.
        pytest.param(
            {_IN},
            {_IN.replace(" in ", "  in ")},
            f"def f():\n{_IN}\n",
            [],
            id="only-spacing",
        ),
        # Different indentation is a different block.
        pytest.param(
            {_IN},
            {"    " + _NOT_IN},
            f"def f():\n{_IN}\n    {_NOT_IN}\n",
            [],
            id="different-indentation",
        ),
        # Prose contradicts prose all the time and no program reads it.
        pytest.param(
            {"    # the path is in deny"},
            {"    # the path is not in deny"},
            "def f():\n    # the path is in deny\n    # the path is not in deny\n",
            [],
            id="a-pair-of-comments",
        ),
        # The `not` is inside a literal, so it is text rather than a negation.
        pytest.param(
            {'    run("do not delete")'},
            {'    run("do delete")'},
            'def f():\n    run("do not delete")\n    run("do delete")\n',
            [],
            id="a-negation-inside-a-literal",
        ),
        # One parent added the whole block, so the merge of the two created
        # nothing — a cherry-pick, or a rename read as a new file.
        pytest.param(
            {_IN, _NOT_IN},
            {_IN, _NOT_IN},
            f"def f():\n{_IN}\n{_NOT_IN}\n",
            [],
            id="a-block-both-parents-added",
        ),
        # The hooks re-space the merged tree before this runs, so the match is
        # whitespace-flattened rather than exact.
        pytest.param(
            {_IN},
            {_NOT_IN},
            f"def f():\n{_IN.replace('deny, deny', 'deny,  deny')}\n{_NOT_IN}\n",
            [2, 3],
            id="a-line-the-hooks-re-spaced",
        ),
        # Different subjects. Asserting one key present and another absent is
        # consistent, so the negation alone must not pair them.
        pytest.param(
            {'    assert "x" in deny'},
            {'    assert "y" not in deny'},
            'def f():\n    assert "x" in deny\n    assert "y" not in deny\n',
            [],
            id="different-literals",
        ),
        # A trailing comment always trails, so dropping it leaves the pair.
        pytest.param(
            {_IN + "  # keep"},
            {_NOT_IN},
            f"def f():\n{_IN}  # keep\n{_NOT_IN}\n",
            [2, 3],
            id="a-trailing-comment",
        ),
        # Two adjacent one-line tests sit three lines apart, so the seam width
        # alone pairs them. Each states something about its own subject, and a
        # merge that took one from each parent chose nothing wrongly.
        pytest.param(
            {_IN},
            {_NOT_IN},
            f"def test_allows():\n{_IN}\n\n\ndef test_denies():\n{_NOT_IN}\n",
            [],
            id="separate-functions",
        ),
        # Two negations cancel, so these two say the same thing. A raw count of
        # negations reads 2 against 0 and calls that a contradiction.
        pytest.param(
            {"    assert not x != y"},
            {"    assert x == y"},
            "def f():\n    assert not x != y\n    assert x == y\n",
            [],
            id="two-negations-cancel",
        ),
    ],
)
def test_a_statement_kept_beside_its_negation(head_added, base_added, merged, want):
    assert contradicting_line_numbers(head_added, base_added, merged) == want


@pytest.mark.parametrize(
    ("names", "want"),
    [
        # One mangled resolution must not fill a pull-request comment.
        (
            [f"_n{i}" for i in range(7)],
            "_n0, _n1, _n2, _n3, _n4, and 2 more",
        ),
        # A Python identifier may hold any word character; `land`'s record
        # grammar is ASCII, and a record it rejects names no file at all.
        (["café", "_ok"], "_ok, and 1 more"),
        (["café", "été"], "2 name(s) this report cannot spell"),
    ],
)
def test_the_name_list_stays_inside_the_record_grammar(names, want):
    assert describe_names(names) == want


def _commit(repo, message: str) -> str:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check=True)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A bound git repository, unbound again when the test ends.

    `_git_io` refuses an unbound call, so the binding is what lets `added_lines`
    run at all — and leaving it bound would hand the next test this checkout.
    """
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
    git_io.bind_repo(root)
    yield root
    git_io._reset_process_state()


def test_a_files_own_plus_lines_stay_under_its_own_path(repo):
    """A file whose CONTENT holds `++ b/x` prints `+++ b/x` inside a hunk, because
    the diff adds its own `+`. Reading that as a header would file the rest of
    this file's additions under a path nothing in the tree carries, and the union
    check would then read one file's additions as another's."""
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "a.py").write_text(
        'x = 1\nSAMPLE = """\n++ b/phantom.py\n"""\nkept = 2\n', encoding="utf-8"
    )
    side = _commit(repo, "side")

    added = added_lines(base, side)
    assert set(added) == {"a.py"}
    assert "kept = 2" in added["a.py"]


def test_a_renamed_file_adds_only_what_the_rename_changed(repo):
    """Under `diff.renames=false` a renamed file reads as a new one whose whole
    inherited body was added, so a pair its ancestor already carried looks like
    one this merge created. `--find-renames` is what makes the reading independent
    of that config rather than of git's current default."""
    (repo / "old.py").write_text("import time\n\n\ndef wait(s):\n    pass\n", "utf-8")
    base = _commit(repo, "base")
    (repo / "old.py").unlink()
    (repo / "new.py").write_text(
        "import time\n\n\ndef wait(s):\n    pass\n\n\nfresh = 1\n", "utf-8"
    )
    side = _commit(repo, "side")

    assert added_lines(base, side) == {"new.py": {"fresh = 1", ""}}


def _criss_cross(repo) -> tuple[str, str]:
    """Two heads with TWO merge bases, each having changed `f.txt` since both.

    `x` and `y` fork from the same commit, then each merges the OTHER's fork
    point. Neither result is an ancestor of the other, so both fork points stay
    equally good ancestors and git builds the real merge from a virtual one."""

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()

    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    _commit(repo, "base")
    forks = {}
    for name in ("x", "y"):
        run("checkout", "-q", "-b", name, "main")
        (repo / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
        forks[name] = _commit(repo, name)
    heads = []
    for name, other in (("x", "y"), ("y", "x")):
        run("checkout", "-q", name)
        run("merge", "-q", "--no-edit", forks[other])
        (repo / "f.txt").write_text(f"{name} side\n", encoding="utf-8")
        heads.append(_commit(repo, f"{name} changes f.txt"))
    return heads[0], heads[1]


def test_a_taken_whole_file_is_declined_on_a_criss_cross_history(repo):
    """Git merges several equally good ancestors into a virtual one, so the base
    this predicate compares the dropped side against names no real commit. A
    finding read off it can blame a change the dropped side never made."""
    head, side = _criss_cross(repo)
    bases = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--all", head, side],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.split()
    assert len(bases) == 2, "fixture must actually criss-cross"

    assert contradictory_merge.taken_whole([head, head, side], ["f.txt"]) == {}


def test_a_taken_whole_file_is_still_reported_on_one_merge_base(repo):
    """The refusing direction for the test above: the same one-sided take over a
    history with a single base still names the drop. Without it the decline above
    passes against a predicate that reports nothing at all."""
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    base = _commit(repo, "base")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side"], check=True)
    (repo / "f.txt").write_text("side\n", encoding="utf-8")
    side = _commit(repo, "side")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    (repo / "f.txt").write_text("head\n", encoding="utf-8")
    head = _commit(repo, "head")

    take = contradictory_merge.taken_whole([head, head, side], ["f.txt"])["f.txt"]
    assert (take.kept, take.dropped, take.base) == (head[:12], side[:12], base[:12])


def test_a_deletion_never_swallows_the_next_files_additions(repo):
    """A deletion's hunk leaves the reader mid-hunk under `+++ /dev/null`. Without
    the per-file reset the NEXT file's own `+++` header reads as an addition, and
    every line it adds is filed under the deleted path."""
    (repo / "gone.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "kept.py").write_text("y = 1\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "gone.py").unlink()
    (repo / "kept.py").write_text("y = 1\nz = 2\n", encoding="utf-8")
    side = _commit(repo, "side")

    assert added_lines(base, side) == {"kept.py": {"z = 2"}}


# agent-glovebox c80ad67d23, reduced: both parents added the same import, and
# the merge kept both copies. Each parent reads cleanly on its own.
_ONE_IMPORT = "import json\n\n\ndef f():\n    return json\n"
_TWO_IMPORTS = "import json\nimport json\n\n\ndef f():\n    return json\n"


@pytest.mark.parametrize(
    ("sides", "merged", "want"),
    [
        pytest.param(
            [_ONE_IMPORT, _ONE_IMPORT],
            _TWO_IMPORTS,
            ["json"],
            id="an-import-kept-twice",
        ),
        pytest.param(
            ["def test_a():\n    pass\n", "def test_a():\n    assert 1\n"],
            "def test_a():\n    pass\n\n\ndef test_a():\n    assert 1\n",
            ["test_a"],
            id="a-test-kept-twice-never-runs-its-first-copy",
        ),
        pytest.param(
            ["LIMIT = 1\n", "LIMIT = 2\n"],
            "LIMIT = 1\nLIMIT = 2\n",
            ["LIMIT"],
            id="a-constant-kept-at-both-values",
        ),
        pytest.param(
            ["class A:\n    pass\n", "class A:\n    x = 1\n"],
            "class A:\n    pass\n\n\nclass A:\n    x = 1\n",
            ["A"],
            id="a-class-kept-twice",
        ),
        # The refusing direction. One copy is what either parent holds.
        pytest.param([_ONE_IMPORT, _ONE_IMPORT], _ONE_IMPORT, [], id="one-copy-kept"),
        # A parent that already shipped two copies: the merge did not add one.
        pytest.param(
            [_TWO_IMPORTS, _ONE_IMPORT], _TWO_IMPORTS, [], id="a-parent-already-had-two"
        ),
        # `import a.b` and `import a.c` both bind `a`, and each stays live.
        pytest.param(
            ["import importlib.util\n", "import importlib.metadata\n"],
            "import importlib.util\nimport importlib.metadata\n",
            [],
            id="two-submodules-of-one-package",
        ),
        # A value that reads the name rebuilds it rather than defining it again.
        pytest.param(
            ["SEEN = set()\n", "SEEN = set()\n"],
            "SEEN = set()\nSEEN = SEEN | {1}\n",
            [],
            id="a-constant-rebuilt-from-itself",
        ),
        # A lowercase name is state a module reassigns on purpose.
        pytest.param(
            ["n = 1\n", "n = 2\n"], "n = 1\nn = 2\n", [], id="lowercase-reassignment"
        ),
        # Defined many times by design.
        pytest.param(
            ["from typing import overload\n"] * 2,
            "from typing import overload\n\n\n@overload\ndef f(x: int) -> int: ...\n"
            "@overload\ndef f(x: str) -> str: ...\n",
            [],
            id="overload-stubs",
        ),
        # A branch that binds a name on one path only is not a top-level copy.
        pytest.param(
            [_ONE_IMPORT, _ONE_IMPORT],
            "import json\ntry:\n    import json\nexcept ImportError:\n    json = None\n",
            [],
            id="a-guarded-import-inside-try",
        ),
        # What the resolution wrote itself, once, is its own code and not a copy.
        pytest.param(
            ["x = 1\n", "x = 2\n"],
            "LIMIT = 3\n",
            [],
            id="a-name-only-the-merge-defines",
        ),
        # `singledispatch` registrations each bind `_` on purpose.
        pytest.param(
            ["x = 1\n", "x = 2\n"],
            "@f.register\ndef _(a: int): ...\n@f.register\ndef _(a: str): ...\n",
            [],
            id="registered-underscores",
        ),
        # The merge still holds a conflict marker: no parser reads it, so the
        # comparison declines rather than guessing.
        pytest.param(
            [_ONE_IMPORT, _ONE_IMPORT],
            "<<<<<<< ours\nimport json\n=======\nimport json\n>>>>>>> theirs\n",
            [],
            id="an-unparseable-merge",
        ),
        # A PARENT no parser read: there is no count to measure the merge against.
        pytest.param(
            ["<<<<<<< ours\nimport json\n", _ONE_IMPORT],
            _TWO_IMPORTS,
            [],
            id="an-unparseable-parent",
        ),
        # A NUL byte, which `ast.parse` refuses with `ValueError` before it parses.
        pytest.param(
            [_ONE_IMPORT, _ONE_IMPORT],
            "import json\x00import json\n",
            [],
            id="a-merge-holding-a-nul-byte",
        ),
    ],
)
def test_a_definition_the_merge_kept_twice(sides, merged, want):
    counts = contradictory_merge.python_definitions
    assert (
        contradictory_merge.duplicated_names(
            [counts(side) for side in sides], counts(merged)
        )
        == want
    )
