"""The out-of-span rewrite gate, driven against hand-built mechanical/resolved
text pairs.

covers: .github/resolver/auto-resolve/_out_of_conflict.py
"""

import pytest

from tests._resolver_helpers import load_script

ooc = load_script(".github/resolver/auto-resolve/_out_of_conflict.py")


def test_conflict_spans_finds_a_single_hunk():
    mechanical = "line1\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\nline2\n"
    assert ooc.conflict_spans(mechanical) == [(2, 6)]


def test_conflict_spans_finds_every_hunk_at_its_own_range():
    mechanical = (
        "a\n<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> b\n"
        "mid\n<<<<<<< HEAD\np\n=======\nq\n>>>>>>> b\nz\n"
    )
    assert ooc.conflict_spans(mechanical) == [(2, 6), (8, 12)]


def test_conflict_spans_covers_the_diff3_base_section():
    mechanical = (
        "<<<<<<< HEAD\nours\n||||||| base\nbased\n=======\ntheirs\n>>>>>>> branch\n"
    )
    assert ooc.conflict_spans(mechanical) == [(1, 7)]


def test_conflict_spans_is_none_for_text_with_no_markers():
    assert ooc.conflict_spans("a\nb\nc\n") is None


@pytest.mark.parametrize(
    "mechanical",
    [
        "<<<<<<< HEAD\nours\n=======\ntheirs\n",
        "<<<<<<< HEAD\nours\n<<<<<<< HEAD\nnested\n=======\ntheirs\n>>>>>>> b\n",
    ],
    ids=["unterminated", "nested-open"],
)
def test_conflict_spans_raises_for_markers_that_do_not_parse(mechanical):
    with pytest.raises(ooc.MalformedMarkersError):
        ooc.conflict_spans(mechanical)


def test_out_of_conflict_hunks_is_empty_when_only_in_span_lines_changed():
    mechanical = "line1\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\nline2\n"
    resolved = "line1\ntheirs\nline2\n"
    assert ooc.out_of_conflict_hunks(mechanical, resolved) == []


def test_out_of_conflict_hunks_flags_a_rewrite_far_outside_the_span():
    # Buffer lines separate the span from the touched comment so difflib
    # reports them as distinct opcodes rather than folding the marker
    # deletion and the re-indent into one `replace` block.
    mechanical = (
        "line1\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
        "buffer1\nbuffer2\n  comment\ntail\n"
    )
    resolved = "line1\ntheirs\nbuffer1\nbuffer2\n        comment\ntail\n"
    violations = ooc.out_of_conflict_hunks(mechanical, resolved)
    assert violations == [ooc.Violation(9, 9, 5, 5)]


def test_out_of_conflict_hunks_flags_a_delete_outside_the_span():
    mechanical = (
        "line1\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
        "buffer1\nbuffer2\nunwanted\ntail\n"
    )
    resolved = "line1\ntheirs\nbuffer1\nbuffer2\ntail\n"
    violations = ooc.out_of_conflict_hunks(mechanical, resolved)
    assert len(violations) == 1
    assert violations[0].mech_start == 9 and violations[0].mech_end == 9


def test_out_of_conflict_hunks_does_not_flag_an_insertion_at_the_span_boundary():
    # Repeating the closing marker's exact text in RESOLVED is an artificial
    # way to force difflib to report a genuine zero-width `insert` opcode
    # sitting right at the span's end, rather than folding the deletion and
    # the new line into one `replace` — the boundary case the slop rule pins.
    mechanical = "line1\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\ntail\n"
    resolved = "line1\ntheirs\n>>>>>>> branch\nNEWLINE\ntail\n"
    assert ooc.out_of_conflict_hunks(mechanical, resolved) == []


def test_out_of_conflict_hunks_does_not_misattribute_a_length_changing_replacement():
    mechanical = "line1\n<<<<<<< HEAD\nours\n=======\ntheirs\nmore theirs\n>>>>>>> branch\ntail\n"
    resolved = "line1\nresolved-one-line\ntail\n"
    assert ooc.out_of_conflict_hunks(mechanical, resolved) == []


def test_out_of_conflict_hunks_is_empty_for_markerless_mechanical_text():
    assert ooc.out_of_conflict_hunks("a\nb\nc\n", "a\nX\nc\n") == []
