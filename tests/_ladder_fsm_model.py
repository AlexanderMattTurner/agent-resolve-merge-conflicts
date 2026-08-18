"""Finite-state model of the auto-resolve credential ladder's retry policy.

The policy it models is `.github/resolver/auto-resolve/_ladder.py`, walked over
the slot list `.github/resolver/auto-resolve/run-ladder.py` builds.
`tests/test_ladder_equivalence.py` proves this walk and that composition agree
on every reachable outcome; `tests/_ladder_fsm_tla.py` prints the same table as
`docs/tla/Ladder.tla`, which TLC checks in CI.

PROBLEM CLASS — a retry policy re-derived as a state machine drifts from the
code it models the moment either side changes alone, and the drift is silent
because both halves still pass their own tests. Two shapes of that drift are
removed here rather than watched:

  * The rung COUNT comes from `lib_credential_ladder.rungs()`, the table the
    workflow itself is generated from. A rung added there changes this model,
    and the freshness test then demands a regenerated `Ladder.tla`.
  * The outcome SYMBOLS are the image of `symbol_of` over all eight
    (errored, zero_cost, wall_clock_only) combinations `claude-run-errored.sh`
    can emit. Those three flags are independent `jq` tests, so a combination
    left out of a hand-written list is a case no theorem covers.

One walk visits the rungs in order. At each rung the walk records an outcome
and `pos` moves to the rung that runs next, or to `DONE`. `pos == "DONE"` is
the only terminal marker: a walk that reached a winner or exhausted its
advances has no further transition to take, so "stopped" needs no separate
flag. `configured<i>` are ordinary state fields, held fixed by every
transition, because each credential slot is set or unset independently.
"""

from itertools import product
from typing import NamedTuple

from tests._fsm_core import (
    T,
    TrSpec,
    ValSpec,
    _compile_machine,
    _eq,
    _is,
    _upd,
)
from tests._fsm_core import explore as _explore
from tests._fsm_core import successors as _successors
from tests._resolver_helpers import load_script

_credentials = load_script(".github/resolver/lib_credential_ladder.py")

RUNGS: tuple[str, ...] = tuple(str(spec.index) for spec in _credentials.rungs())
POS_VALUES: tuple[str, ...] = (*RUNGS, "DONE")

# The rungs `run-ladder.py`'s `_slots()` keeps whatever their own secret says:
# rung 1 is the ladder's entry, and the rung that reuses its predecessor's
# credential is the free same-credential retry. Every other rung is DROPPED
# when unset, which is why an error steps over a gap rather than stopping at it.
ALWAYS_WALKED: frozenset[str] = frozenset({"1"}) | {
    str(spec.index)
    for spec in _credentials.rungs()
    if spec.reuses_predecessor_credential
}
if ALWAYS_WALKED != {"1", "2"}:
    # The transitions below encode `advances`' index-0 asymmetry as a rule about
    # rung 1, and its boundary as a rule about rung 2. Refuse rather than emit a
    # machine whose special cases sit on the wrong rungs.
    raise SystemExit(
        "the credential ladder's always-walked rungs are"
        f" {sorted(ALWAYS_WALKED)}, and this model's rung-1 and rung-2 rules"
        " assume {'1', '2'}. Update the transitions before the table."
    )


def symbol_of(errored: bool, zero_cost: bool, wall_clock_only: bool) -> str:
    """The outcome symbol for one `RungOutcome`.

    Two collapses, each because the policy cannot read the flag there:
    `advances` returns False on a run that did not error before it ever looks at
    `wall_clock_only`, so a successful run's wall flag changes nothing; and
    `evaluate`'s release rule reads `zero_cost` alone, never `errored`.
    """
    if not errored:
        return "OK_ZERO" if zero_cost else "OK"
    if wall_clock_only:
        return "ERR_WALL_ZERO" if zero_cost else "ERR_WALL"
    return "ERR_ZERO" if zero_cost else "ERR_PAID"


ALL_FLAGS: tuple[tuple[bool, bool, bool], ...] = tuple(product((False, True), repeat=3))
# `dict.fromkeys` for an ordered set: the emitted TLA+ domain must be stable
# across runs, and a `set` here would reorder it between interpreters.
SYMBOLS: tuple[str, ...] = tuple(
    dict.fromkeys(symbol_of(*flags) for flags in ALL_FLAGS)
)
OUTCOME_VALUES: tuple[str, ...] = ("NOT_RUN", *SYMBOLS)
# The symbols each predicate of the policy reads, derived from the same flags so
# a new symbol cannot miss one.
ZERO_COST_OUTCOMES: frozenset[str] = frozenset(
    symbol_of(*flags) for flags in ALL_FLAGS if flags[1]
)
ERRORED_OUTCOMES: frozenset[str] = frozenset(
    symbol_of(*flags) for flags in ALL_FLAGS if flags[0]
)
WINNER_VALUES: tuple[str, ...] = ("NONE", *RUNGS)

_FIELDS: list[tuple[str, type]] = [
    ("pos", str),
    *((f"o{i}", str) for i in RUNGS),
    ("winner", str),
    *((f"configured{i}", bool) for i in RUNGS[1:]),
]
Lg = NamedTuple("Lg", _FIELDS)  # type: ignore[misc]
Lg.__doc__ = (
    "One walk's configuration: the rung about to run (or `DONE`), each rung's"
    " recorded outcome, the winner (or `NONE`), and whether each rung past the"
    " first has its own credential configured. Rung 1 has no `configured` field:"
    " the ladder always has a primary credential."
)

FIELD_DOMAINS: dict[str, tuple[object, ...]] = {
    "pos": POS_VALUES,
    **{f"o{i}": OUTCOME_VALUES for i in RUNGS},
    "winner": WINNER_VALUES,
    **{f"configured{i}": (False, True) for i in RUNGS[1:]},
}


def start(*configured: bool) -> Lg:
    """A fresh walk: nothing has run, over the rung-2 onwards CONFIGURED flags
    in order."""
    if len(configured) != len(RUNGS) - 1:
        raise ValueError(f"expected {len(RUNGS) - 1} flags, got {len(configured)}")
    return Lg(
        pos="1",
        **{f"o{i}": "NOT_RUN" for i in RUNGS},
        winner="NONE",
        **{f"configured{i}": flag for i, flag in zip(RUNGS[1:], configured)},
    )


def _next_configured(i: str) -> ValSpec:
    """The rung the walk runs after an error at rung I, or `DONE`.

    `_slots()` drops an unconfigured rung before the walk begins, so `evaluate`
    never sees a gap: an error steps to the next rung that HAS its own
    credential, however many unset ones sit between. Reading only the immediate
    successor would stop at the first gap and model a ladder that never reaches
    the credentials behind it.
    """
    spec: ValSpec = ("lit", "DONE")
    for j in reversed(RUNGS[RUNGS.index(i) + 1 :]):
        spec = ("cond", _is(f"configured{j}"), ("lit", j), spec)
    return spec


def _advance(i: str, errored: bool, zero_cost: bool, wall: bool) -> ValSpec:
    """Where `pos` goes after rung I records this outcome — `_ladder.advances`,
    as a value spec over the walk's own state."""
    if not errored or wall:
        return ("lit", "DONE")
    if i == "1":
        # Index 0's asymmetry: the free retry runs on rung 1's own credential, so
        # a zero-billed error advances whether or not rung 2 has its own secret.
        # Rung 2 is always in the walked list, so the boundary reads its flag
        # alone rather than searching past it.
        nxt = RUNGS[1]
        return (
            ("lit", nxt)
            if zero_cost
            else ("cond", _is(f"configured{nxt}"), ("lit", nxt), ("lit", "DONE"))
        )
    return _next_configured(i)


def _transition(i: str, flags: tuple[bool, bool, bool]) -> TrSpec:
    errored, zero_cost, wall = flags
    symbol = symbol_of(*flags)
    winner_updates = {} if errored else {"winner": i}
    return TrSpec(
        f"run_{i}_{symbol.lower()}",
        (_eq("pos", i),),
        (
            "update",
            (
                *_upd(**{f"o{i}": symbol}, **winner_updates),
                ("pos", _advance(i, errored, zero_cost, wall)),
            ),
        ),
    )


# One transition per rung per SYMBOL, not per flag combination: two flag
# combinations that `symbol_of` collapses produce the identical step, and the
# duplicate would emit a second TLA+ action with the same body.
LADDER_TRANSITIONS: tuple[TrSpec, ...] = tuple(
    _transition(i, next(f for f in ALL_FLAGS if symbol_of(*f) == symbol))
    for i in RUNGS
    for symbol in SYMBOLS
)

LADDER: list[T] = _compile_machine(LADDER_TRANSITIONS)


def successors(s: Lg) -> list[tuple[str, Lg]]:
    return _successors(s, LADDER)


def reachable(s: Lg) -> set[Lg]:
    """Every walk state reachable from S, which is always DONE-terminated:
    every branch of every `run_*` transition strictly advances `pos`, so no
    cycle exists to make this walk unbounded."""
    return _explore([s], successors)


def ran(s: Lg) -> tuple[str, ...]:
    """The rungs that ran, in order — the tuple `evaluate` reports as
    `LadderVerdict.ran`."""
    return tuple(i for i in RUNGS if getattr(s, f"o{i}") != "NOT_RUN")


def released(s: Lg) -> bool:
    """`evaluate`'s release rule: at least one rung ran, and every rung that ran
    billed nothing. The rule reads `zero_cost` alone and never `errored`, so a
    zero-billed WINNER releases the mark, and so does a zero-billed
    wall-clock-only failure."""
    ran_rungs = ran(s)
    return bool(ran_rungs) and all(
        getattr(s, f"o{i}") in ZERO_COST_OUTCOMES for i in ran_rungs
    )
