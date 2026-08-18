"""The FSM walk in `tests/_ladder_fsm_model.py` and the shipped policy in
`.github/resolver/auto-resolve/_ladder.py` decide every walk the same way.

PROBLEM CLASS — a retry policy re-derived as a state machine drifts from the
function it models the moment either side changes alone. `docs/tla/Ladder.tla`
is generated from that state machine, so every theorem TLC proves there is a
theorem about the shipped policy only while this comparison holds.

The comparison is exhaustive over the model's whole reachable set: 64 rung-2..7
CONFIGURED combinations, every walk each one admits. For each terminal walk the
test hands `evaluate` a FULL seven-rung outcome table — the walk's own recorded
outcomes, and `OK` for every rung the walk never reached. `OK` is the filler
because it is the one symbol that ends `evaluate`'s loop with a winner, so a
policy that runs one rung too far reports a longer `ran` and a different
`winner` instead of quietly agreeing.
"""

from tests import _ladder_fsm_model as model
from tests._resolver_helpers import load_script

ladder = load_script(".github/resolver/auto-resolve/_ladder.py")

# The model's four outcome symbols, as the RungOutcome each one stands for.
# `ERR_WALL` is a proven wall-clock-only failure, which is billed: the model
# carries no zero-cost wall-clock symbol, so neither does this table.
OUTCOMES = {
    "OK": ladder.RungOutcome(errored=False, zero_cost=False),
    "ERR_ZERO": ladder.RungOutcome(errored=True, zero_cost=True),
    "ERR_PAID": ladder.RungOutcome(errored=True, zero_cost=False),
    "ERR_WALL": ladder.RungOutcome(errored=True, zero_cost=False, wall_clock_only=True),
}


def rungs_of(state) -> list:
    """The seven-slot rung table STATE's CONFIGURED fields describe. Rung 1 is
    always configured: the ladder always has a primary credential."""
    return [
        ladder.Rung(
            name=f"rung_{i}",
            token_env=f"TOKEN_{i}",
            configured=i == "1" or getattr(state, f"configured{i}"),
        )
        for i in model.RUNGS
    ]


def outcomes_of(state) -> dict:
    """A full seven-rung outcome table: what the walk recorded, then `OK`."""
    return {f"rung_{i}": OUTCOMES[_recorded(state, i)] for i in model.RUNGS}


def _recorded(state, i: str) -> str:
    outcome = getattr(state, f"o{i}")
    return "OK" if outcome == "NOT_RUN" else outcome


def terminal_states() -> list:
    """Every reachable walk that has stopped, over all 64 configurations."""
    reached: set = set()
    for combo in range(64):
        flags = tuple(bool(combo >> bit & 1) for bit in range(6))
        reached |= set(model.reachable(model.start(*flags)))
    return sorted(s for s in reached if s.pos == "DONE")


TERMINAL = terminal_states()


def test_the_reachable_set_is_not_empty_so_this_comparison_is_not_vacuous():
    # A start state with no successors would make every assertion below pass
    # over nothing, which is the failure mode this whole file exists to catch.
    assert len(TERMINAL) > 500, len(TERMINAL)


def test_the_policy_stops_where_the_model_stops():
    for state in TERMINAL:
        verdict = ladder.evaluate(rungs_of(state), outcomes_of(state))
        want = tuple(f"rung_{i}" for i in model.ran(state))
        assert verdict.ran == want, state


def test_the_policy_names_the_winner_the_model_names():
    for state in TERMINAL:
        verdict = ladder.evaluate(rungs_of(state), outcomes_of(state))
        want = None if state.winner == "NONE" else f"rung_{state.winner}"
        assert verdict.winner == want, state


def test_the_policy_releases_the_mark_exactly_when_the_model_does():
    for state in TERMINAL:
        verdict = ladder.evaluate(rungs_of(state), outcomes_of(state))
        assert verdict.release_attempt is model.released(state), state
