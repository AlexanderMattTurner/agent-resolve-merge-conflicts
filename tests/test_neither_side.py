"""The in-conflict provenance report, driven against hand-built mechanical and
resolved text pairs.

covers: .github/resolver/auto-resolve/_neither_side.py
"""

import pytest

from tests._resolver_helpers import load_script

neither = load_script(".github/resolver/auto-resolve/_neither_side.py")

# One region, `ours` against `theirs`, with a context line on each side of it.
_MECHANICAL = "head\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\ntail\n"


def test_neither_side_names_the_line_no_side_wrote():
    resolved = "head\nsomething else\ntail\n"
    assert neither.lines_from_neither_side(_MECHANICAL, resolved) == [2]


def test_neither_side_names_nothing_when_the_resolution_takes_one_side():
    assert neither.lines_from_neither_side(_MECHANICAL, "head\ntheirs\ntail\n") == []


def test_neither_side_names_nothing_for_a_union_of_both_sides():
    resolved = "head\ntheirs\nours\ntail\n"
    assert neither.lines_from_neither_side(_MECHANICAL, resolved) == []


def test_neither_side_names_a_line_the_resolution_restored_from_the_merge_base():
    """The `|||||||` section is the ancestor, so it belongs to neither parent.
    Both sides changed that text, and putting it back ships a line no parent
    carries."""
    mechanical = (
        "head\n<<<<<<< HEAD\nours\n||||||| base\nancestor\n"
        "=======\ntheirs\n>>>>>>> branch\ntail\n"
    )
    assert neither.lines_from_neither_side(mechanical, "head\nancestor\ntail\n") == [2]


def test_neither_side_names_nothing_for_a_line_the_merge_holds_as_context():
    """A resolution may repeat a line the mechanical merge already holds outside
    every region: somebody wrote it, so it traces."""
    assert neither.lines_from_neither_side(_MECHANICAL, "head\ntail\ntail\n") == []


def test_neither_side_ignores_a_blank_line_the_resolution_added():
    """`_widened.revert_whitespace_only_edits` owns that class, and reporting one
    here would disarm auto-merge on every resolution that spaces its answer."""
    assert neither.lines_from_neither_side(_MECHANICAL, "head\nours\n   \ntail\n") == []


def test_neither_side_names_nothing_for_markerless_mechanical_text():
    """A whole-file `-merge` keep or a binary has no region to compare against."""
    assert neither.lines_from_neither_side("a\nb\n", "a\nX\n") == []


@pytest.mark.parametrize(
    "mechanical",
    [
        "<<<<<<< HEAD\nours\n=======\ntheirs\n",
        "<<<<<<< HEAD\nours\n<<<<<<< HEAD\nnested\n=======\ntheirs\n>>>>>>> b\n",
    ],
    ids=["unterminated", "nested-open"],
)
def test_neither_side_raises_for_markers_that_do_not_parse(mechanical):
    """The most suspicious input this reads. Answering "nothing to report" would
    fail the report open on exactly the tree it should be strictest about."""
    with pytest.raises(neither.MalformedMarkersError):
        neither.lines_from_neither_side(mechanical, "anything\n")


def test_neither_side_describes_a_run_of_lines_as_one_range():
    assert neither.describe([3, 4, 5, 9]) == "3-5, 9"


def test_neither_side_truncates_a_long_range_list_with_a_count():
    """`land` parses this text into a pull-request comment, so one mangled
    resolution must not be able to fill it."""
    assert neither.describe([1, 3, 5, 7, 9, 11, 13]) == "1, 3, 5, 7, 9, and 2 more"
