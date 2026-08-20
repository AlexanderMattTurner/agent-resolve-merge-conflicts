"""Finite-state model of one auto-resolve run, ending in the verdict it reports.

The policy it models is `.github/resolver/auto-resolve/outcome.py`.
`tests/test_outcome_equivalence.py` proves this machine's terminal verdict equals
that module's over every reachable state; `tests/_outcome_fsm_tla.py` prints the
same table as `docs/tla/AutoResolve.tla`, which TLC checks in CI.

PROBLEM CLASS — a run that left the conflict standing and reported success. The
verdict function is a table over four small enums, so the question TLC answers is
COMPLETENESS: is there a reachable ending where the conflict is still there, no
other run is on the hook for it, and the run reports success? `AutoResolve.tla`'s
`ConflictStandsImpliesStall` is that question, and it fails the moment a new
land ending or a new claim joins the enums without an arm.

A run walks one phase chain: it decides whether discover selected the pull
request, then what the attempt mark said, then what the resolve job published,
then how the land job ended. `phase == "DONE"` is the only terminal marker, since
every transition strictly advances the chain. Each terminal transition writes the
verdict, which is what makes the verdict a property of the reachable state rather
than of a function nothing checks.

The enum MEMBERS come from `outcome.py` itself, so a member added there widens
this model and the freshness test then demands a regenerated module.
"""

from typing import NamedTuple

from tests._fsm_core import T, TrSpec, ValSpec, _compile_machine, _eq, _upd
from tests._fsm_core import explore as _explore
from tests._fsm_core import successors as _successors
from tests._resolver_helpers import load_script

outcome = load_script(".github/resolver/auto-resolve/outcome.py")

CLAIMS: tuple[str, ...] = tuple(member.value.upper() for member in outcome.Claim)
PUBLISHED: tuple[str, ...] = tuple(member.value.upper() for member in outcome.Published)
LANDS: tuple[str, ...] = tuple(member.value.upper() for member in outcome.Land)
PHASES: tuple[str, ...] = ("SELECT", "CLAIM", "RESOLVE", "LAND", "DONE")

# The rule, RE-DERIVED here rather than read from `outcome.verdict`. A model that
# asks the code it models proves only that its own interpreter works, so this is
# the twin `tests/test_outcome_equivalence.py` compares against — the same
# arrangement `_ladder_fsm_model.symbol_of` has with the shipped ladder.
#
# Read top to bottom, like the arms it models: who else carries the conflict
# outranks what this run managed to do.
_STAND_DOWN_VERDICT: dict[str, str] = {
    "NONE": "gave_up",
    "DUPLICATE": "duplicate",
    "LATCHED": "latched",
}
_LAND_VERDICT: dict[str, str] = {
    "PUSHED": "landed",
    "SUPERSEDED": "superseded",
    "NOT_NEEDED": "already_clear",
    "QUEUE_HELD": "held",
    "FAILED": "land_failed",
}
_PUBLISHED_VERDICT: dict[str, str] = {
    "NO_OP": "no_op",
    "HANDOFF": "handed_off",
    "DECLINE": "handed_off",
}
# The verdicts that mean the conflict is still there and nothing else is on the
# hook for it. The gate exits non-zero on exactly these.
STALLS: frozenset[str] = frozenset({"latched", "land_failed", "handed_off", "gave_up"})


def verdict_of(selected: bool, claim: str, published: str, land: str) -> str:
    """This model's own name for a run with these four facts."""
    if not selected:
        return "refused"
    if claim in _STAND_DOWN_VERDICT and claim != "NONE":
        return _STAND_DOWN_VERDICT[claim]
    if land in _LAND_VERDICT:
        return _LAND_VERDICT[land]
    return _PUBLISHED_VERDICT.get(published, "gave_up")


VERDICTS: tuple[str, ...] = (
    "NONE",
    *dict.fromkeys(
        verdict_of(selected, claim, published, land)
        for selected in (False, True)
        for claim in CLAIMS
        for published in PUBLISHED
        for land in LANDS
    ),
)

_FIELDS: list[tuple[str, type]] = [
    ("phase", str),
    ("selected", bool),
    ("claim", str),
    ("published", str),
    ("land", str),
    ("verdict", str),
]
Run = NamedTuple("Run", _FIELDS)  # type: ignore[misc]
Run.__doc__ = (
    "One run's record: how far it got, what each job observed, and the verdict"
    " the outcome gate reports for it."
)

FIELD_DOMAINS: dict[str, tuple[object, ...]] = {
    "phase": PHASES,
    "selected": (False, True),
    "claim": CLAIMS,
    "published": PUBLISHED,
    "land": LANDS,
    "verdict": VERDICTS,
}

START = Run(
    phase="SELECT",
    selected=False,
    claim="NONE",
    published="NONE",
    land="NOT_RUN",
    verdict="NONE",
)


def _land_verdict(claim: str, land: str) -> ValSpec:
    """Where a run ends after this land ending, as a value spec over `published`.

    A land ending that decides the verdict by itself is a literal. The two that
    push nothing and name nobody else — no bundle, and a land job that never ran
    — read `published` instead, which is the chain of conditions TLC evaluates.
    """
    by_published = {p: verdict_of(True, claim, p, land) for p in PUBLISHED}
    distinct = set(by_published.values())
    if len(distinct) == 1:
        return ("lit", distinct.pop())
    spec: ValSpec = ("lit", by_published[PUBLISHED[-1]])
    for published in reversed(PUBLISHED[:-1]):
        spec = (
            "cond",
            _eq("published", published),
            ("lit", by_published[published]),
            spec,
        )
    return spec


def _transitions() -> tuple[TrSpec, ...]:
    steps = [
        TrSpec(
            "select_none",
            (_eq("phase", "SELECT"),),
            (
                "update",
                _upd(
                    selected=False,
                    phase="DONE",
                    verdict=verdict_of(False, "NONE", "NONE", "NOT_RUN"),
                ),
            ),
        ),
        TrSpec(
            "select_pr",
            (_eq("phase", "SELECT"),),
            ("update", _upd(selected=True, phase="CLAIM")),
        ),
    ]
    # Every claim reaches the LAND phase, including a stand-down: the land job's
    # own condition is that discover SELECTED the pull request, so it runs and
    # writes an ending even when the resolve job spent nothing. A model that
    # stopped a stand-down at CLAIM would exclude states production reaches.

    # NONE goes on to RESOLVE like OWNED, because it is the REUSE HIT: that path
    # skips the mark step entirely, re-publishes a prior run's artifact, and land
    # pushes it. Ending it at CLAIM would hide the commonest non-OWNED run.
    for claim in CLAIMS:
        steps.append(
            TrSpec(
                f"claim_{claim.lower()}",
                (_eq("phase", "CLAIM"),),
                (
                    "update",
                    _upd(
                        claim=claim,
                        phase="RESOLVE" if claim in ("OWNED", "NONE") else "LAND",
                    ),
                ),
            )
        )
    for published in PUBLISHED:
        steps.append(
            TrSpec(
                f"publish_{published.lower()}",
                (_eq("phase", "RESOLVE"),),
                ("update", _upd(published=published, phase="LAND")),
            )
        )
    # One land transition per (claim, ending). The claim is in the name because a
    # stand-down's verdict does not read the ending at all, so the two families
    # write different values from the same phase.
    for claim in CLAIMS:
        for land in LANDS:
            steps.append(
                TrSpec(
                    f"land_{claim.lower()}_{land.lower()}",
                    (_eq("phase", "LAND"), _eq("claim", claim)),
                    (
                        "update",
                        (
                            *_upd(land=land, phase="DONE"),
                            ("verdict", _land_verdict(claim, land)),
                        ),
                    ),
                )
            )
    return tuple(steps)


OUTCOME_TRANSITIONS: tuple[TrSpec, ...] = _transitions()
OUTCOME: list[T] = _compile_machine(OUTCOME_TRANSITIONS)


def successors(s: Run) -> list[tuple[str, Run]]:
    return _successors(s, OUTCOME)


def reachable(s: Run = START) -> set[Run]:
    """Every run state reachable from S. Always DONE-terminated: every transition
    strictly advances the phase chain, so no cycle exists."""
    return _explore([s], successors)
