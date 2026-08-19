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

# Every name `outcome.verdict` can return, plus the not-yet-decided marker. Taken
# by running the function rather than by listing the arms, so a renamed verdict
# cannot leave a stale domain behind.
_FACTS = [
    outcome.RunFacts(selected, claim, published, land)
    for selected in (False, True)
    for claim in outcome.Claim
    for published in outcome.Published
    for land in outcome.Land
]
VERDICTS: tuple[str, ...] = (
    "NONE",
    *dict.fromkeys(outcome.verdict(f).name for f in _FACTS),
)
# The verdicts that mean the conflict is still there and nothing else is on the
# hook for it. `outcome.verdict` decides which, so the model cannot disagree.
STALLS: frozenset[str] = frozenset(
    outcome.verdict(f).name for f in _FACTS if outcome.verdict(f).stall
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


def _named(value: str, enum) -> object:
    """The enum member whose value is VALUE lowercased — the model spells the
    members in upper case, because TLA+ reads them as bare strings."""
    return enum(value.lower())


def _verdict_of(claim: str, published: str, land: str) -> str:
    """`outcome.verdict`'s name for a SELECTED run with these three facts."""
    return outcome.verdict(
        outcome.RunFacts(
            selected=True,
            claim=_named(claim, outcome.Claim),
            published=_named(published, outcome.Published),
            land=_named(land, outcome.Land),
        )
    ).name


def _land_verdict(land: str) -> ValSpec:
    """Where a run that owned the head lands, as a value spec over `published`.

    A land ending that decides the verdict by itself is a literal. The two that
    push nothing and name nobody else — no bundle, and a land job that never ran
    — read `published` instead, which is the chain of conditions TLC evaluates.
    """
    by_published = {p: _verdict_of("OWNED", p, land) for p in PUBLISHED}
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
                    verdict=outcome.verdict(
                        outcome.RunFacts(
                            False,
                            outcome.Claim.NONE,
                            outcome.Published.NONE,
                            outcome.Land.NOT_RUN,
                        )
                    ).name,
                ),
            ),
        ),
        TrSpec(
            "select_pr",
            (_eq("phase", "SELECT"),),
            ("update", _upd(selected=True, phase="CLAIM")),
        ),
    ]
    # A claim that stands the run down ends it; only OWNED goes on to resolve.
    for claim in CLAIMS:
        if claim == "OWNED":
            steps.append(
                TrSpec(
                    "claim_owned",
                    (_eq("phase", "CLAIM"),),
                    ("update", _upd(claim="OWNED", phase="RESOLVE")),
                )
            )
            continue
        steps.append(
            TrSpec(
                f"claim_{claim.lower()}",
                (_eq("phase", "CLAIM"),),
                (
                    "update",
                    _upd(
                        claim=claim,
                        phase="DONE",
                        verdict=_verdict_of(claim, "NONE", "NOT_RUN"),
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
    for land in LANDS:
        steps.append(
            TrSpec(
                f"land_{land.lower()}",
                (_eq("phase", "LAND"),),
                (
                    "update",
                    (
                        *_upd(land=land, phase="DONE"),
                        ("verdict", _land_verdict(land)),
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
