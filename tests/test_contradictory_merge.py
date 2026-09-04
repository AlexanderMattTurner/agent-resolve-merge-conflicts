"""The three checks that read a merge whose every line traces to a parent.

covers: .github/resolver/auto-resolve/_contradictory_merge.py

Each check is a heuristic, so its FILTERS are the contract — a case below that
stops firing is a false positive shipped to every resolution. The three motivating
merges are agent-glovebox #5568 (a split rename), #5641 (a revert undone) and
#5606 (a statement beside its negation).
"""

import pytest

from tests._resolver_helpers import load_script

contradictory_merge = load_script(
    ".github/resolver/auto-resolve/_contradictory_merge.py"
)
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
    ],
)
def test_a_statement_kept_beside_its_negation(head_added, base_added, merged, want):
    assert contradicting_line_numbers(head_added, base_added, merged) == want


def test_the_name_list_truncates_with_a_count():
    """One mangled resolution must not fill a pull-request comment."""
    assert describe_names([f"_n{i}" for i in range(7)]) == (
        "_n0, _n1, _n2, _n3, _n4, and 2 more"
    )
