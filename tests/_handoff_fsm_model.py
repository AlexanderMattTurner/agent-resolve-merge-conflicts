"""Finite-state model of a HEAD across two auto-resolve runs: how the first one
ended, whether that wrote the handoff attempt mark, and what the second run then
does.

The policy it models is `.github/resolver/auto-resolve/_refusal.py`'s `fail`,
whose one branch — `if not resolver_fault: mark_handed_off(...)` — decides the
mark. `tests/test_handoff_equivalence.py` proves this machine's rule equals that
module's over every ending; `tests/_handoff_fsm_tla.py` prints the same table as
`docs/tla/Handoff.tla`, which TLC checks in CI.

PROBLEM CLASS — a run that failed for a reason the TREE did not cause, and marked
the head anyway. The mark exists to stop the resolver paying an LLM again for an
answer whose inputs did not change, so it is right for a merge the model could
not do and wrong for a binary this job never installed: the second is fixed
outside the pull request, and a re-run against the same head then answers
differently. A wrongly marked head instead stands every later scan down until the
mark's TTL expires. `AutoResolve.tla` cannot see this — it models ONE run, and
carries no mark and no cause.

The single-run models beside this one end at a verdict. This one starts there,
because the damage is entirely in the NEXT run: run 1 reports the same `gave_up`
either way.
"""

from typing import NamedTuple

from tests._fsm_core import T, TrSpec, ValSpec, _compile_machine, _eq, _upd
from tests._fsm_core import explore as _explore
from tests._fsm_core import successors as _successors

# How run 1 ended, as `_refusal.fail`'s callers distinguish them. LANDED is the
# run that never calls `fail` at all; the other two are its `resolver_fault`
# argument, which is the whole of the rule below.
ENDINGS: tuple[str, ...] = ("LANDED", "MERGE", "PLUMBING")
CAUSES: tuple[str, ...] = ("NONE", *ENDINGS)
RETRIES: tuple[str, ...] = ("NOT_RUN", "RESOLVES", "STOOD_DOWN")
PHASES: tuple[str, ...] = ("RUN1", "MARK", "RETRY", "DONE")


# The endings THE TREE ITSELF CAUSED: a re-run against the same head reproduces
# them, because their input is the conflict. Stated here as the SPECIFICATION —
# what a mark would be right for — and deliberately NOT derived from the mark
# rule below, which is the implementation. `docs/tla/Handoff.tla` checks one
# against the other, so a rule that grew to mark a plumbing fault reds TLC.
TREE_CAUSED: frozenset[str] = frozenset({"MERGE"})


def marks_head(cause: str) -> bool:
    """Whether a run that ended for CAUSE writes the handoff attempt mark.

    RE-DERIVED here rather than read from `_refusal.fail`, so the equivalence
    test compares two independent statements of one rule. A model that asks the
    code it models proves only that its own interpreter works.
    """
    return cause == "MERGE"


def retry_of(marked: bool) -> str:
    """What the SECOND run does against a head in this state. `discover` reads the
    mark and stands the run down; an unmarked head is an ordinary candidate."""
    return "STOOD_DOWN" if marked else "RESOLVES"


class Head(NamedTuple):
    """One head's record: how far this model got, how run 1 ended, whether the
    mark was written, and what run 2 did with it."""

    phase: str
    cause: str
    marked: bool
    retry: str


FIELD_DOMAINS: dict[str, tuple[object, ...]] = {
    "phase": PHASES,
    "cause": CAUSES,
    "marked": (False, True),
    "retry": RETRIES,
}

START = Head(phase="RUN1", cause="NONE", marked=False, retry="NOT_RUN")


def _mark_value() -> ValSpec:
    """`marked`, as a chain of conditions on `cause` that TLC evaluates — so the
    rule is a property of the reachable set rather than of a function nothing
    checks. Written from `marks_head`, so an ending added to `ENDINGS` joins the
    chain by itself."""
    spec: ValSpec = ("lit", marks_head(ENDINGS[-1]))
    for cause in reversed(ENDINGS[:-1]):
        spec = ("cond", _eq("cause", cause), ("lit", marks_head(cause)), spec)
    return spec


def _transitions() -> tuple[TrSpec, ...]:
    steps = [
        TrSpec(
            f"end_{cause.lower()}",
            (_eq("phase", "RUN1"),),
            ("update", _upd(cause=cause, phase="MARK")),
        )
        for cause in ENDINGS
    ]
    steps.append(
        TrSpec(
            "write_the_mark",
            (_eq("phase", "MARK"),),
            ("update", (*_upd(phase="RETRY"), ("marked", _mark_value()))),
        )
    )
    # One transition per MARK state, not one that reads `marks_head` again: the
    # second run sees only the mark, so a model that re-consulted the cause here
    # would prove the rule against itself instead of against what run 2 can read.
    for marked in (False, True):
        steps.append(
            TrSpec(
                "retry_after_a_mark" if marked else "retry_with_no_mark",
                (_eq("phase", "RETRY"), _eq("marked", marked)),
                ("update", _upd(retry=retry_of(marked), phase="DONE")),
            )
        )
    return tuple(steps)


HANDOFF_TRANSITIONS: tuple[TrSpec, ...] = _transitions()
HANDOFF: list[T] = _compile_machine(HANDOFF_TRANSITIONS)


def successors(s: Head) -> list[tuple[str, Head]]:
    return _successors(s, HANDOFF)


def reachable(s: Head = START) -> set[Head]:
    """Every head state reachable from S. Always DONE-terminated: every transition
    strictly advances the phase chain, so no cycle exists."""
    return _explore([s], successors)
