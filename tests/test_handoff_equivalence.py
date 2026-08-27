"""The FSM in `tests/_handoff_fsm_model.py` marks a head exactly as the shipped
refusal does.

PROBLEM CLASS — a policy re-derived as a state machine drifts from the code it
models the moment either side changes alone. `docs/tla/Handoff.tla` is generated
from that state machine, so `FaultNeverStrandsTheHead` is a theorem about the
shipped refusal only while this comparison holds.

What this proves: for each ending `fail` can be CALLED with, the mark is written
if and only if `marks_head` says so. What it does not: the model's LANDED arm,
which is the run that reaches no `fail` at all — a landed resolution returns
before any refusal, so there is no call to compare against.
"""

import pytest

from tests._resolver_helpers import load_script

# covers: .github/resolver/auto-resolve/_refusal.py
from tests import _handoff_fsm_model as head

refusal = load_script(".github/resolver/auto-resolve/_refusal.py")

# How to DRIVE `fail` into each ending. `fail`'s own docstring draws the first
# line: the default blames the MERGE, which a re-run reproduces, and
# `resolver_fault` blames this job's grants, tooling or workflow plumbing.
# SUPERSEDED is not an argument at all — it is a push that replaced the head, which
# `fail` reads for itself, so the driver answers `superseding_head` instead.
DRIVEN_ENDINGS = ("MERGE", "PLUMBING", "SUPERSEDED")


@pytest.fixture
def marks(monkeypatch) -> list[bool]:
    """Whether `fail` reached `mark_handed_off`, with everything else it does to
    the outside world stubbed: the merge abort, the superseded-head read, and the
    sticky comment are not what this comparison is about."""
    written: list[bool] = []
    monkeypatch.setattr(
        refusal, "mark_handed_off", lambda **_: written.append(True), raising=True
    )
    monkeypatch.setattr(refusal, "abort_merge_if_in_progress", lambda: None)
    monkeypatch.setattr(refusal, "superseding_head", lambda: "")
    monkeypatch.setattr(refusal, "_flush_inherited_stdio", lambda: None)
    monkeypatch.setattr(refusal.subprocess, "run", lambda *a, **k: None)
    return written


@pytest.mark.parametrize("ending", sorted(DRIVEN_ENDINGS))
def test_the_model_marks_a_head_exactly_when_the_shipped_refusal_does(
    ending: str, marks: list[bool], monkeypatch
) -> None:
    if ending == "SUPERSEDED":
        monkeypatch.setattr(refusal, "superseding_head", lambda: "b0bacafe")
    with pytest.raises(SystemExit):
        refusal.fail(
            f"a {ending} ending",
            "the comment this run publishes",
            resolver_fault=ending == "PLUMBING",
        )
    assert bool(marks) == head.marks_head(ending)


def test_every_ending_the_refusal_distinguishes_is_in_the_model() -> None:
    """The comparison covers the whole of `fail`'s rule, not a sample of it: a
    third `resolver_fault`-like argument added there would leave an ending this
    test never drives, and the theorem would then be about less than the code."""
    assert set(DRIVEN_ENDINGS) <= set(head.ENDINGS)
    assert set(head.ENDINGS) - set(DRIVEN_ENDINGS) == {"LANDED"}


def test_the_endings_disagree_so_the_comparison_is_not_vacuous() -> None:
    """A rule that answered the same for every ending would pass the parametrized
    test above while proving nothing. The mark's whole purpose is that it separates
    the merge from the two a re-run answers differently."""
    assert head.marks_head("MERGE")
    assert not head.marks_head("PLUMBING")
    assert not head.marks_head("SUPERSEDED")
