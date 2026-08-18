"""The FSM walk in `tests/_ladder_fsm_model.py` decides every walk exactly as
the shipped ladder does.

PROBLEM CLASS — a retry policy re-derived as a state machine drifts from the
code it models the moment either side changes alone. `docs/tla/Ladder.tla` is
generated from that state machine, so every theorem TLC proves there is a
theorem about the shipped ladder only while this comparison holds.

The comparison drives the real COMPOSITION, not `_ladder.evaluate` alone:
`run-ladder.py`'s `_slots()` builds the rung list from the `RUNG_<i>_TOKEN`
environment, dropping an unconfigured rung, and `main()` maps each slot to a
`Rung`. A test that built its own seven-slot list would prove agreement about a
ladder shape production never constructs — which is how the gap-skipping rule
went unmodelled.

For each terminal walk the test hands `evaluate` a FULL outcome table: the
walk's own recorded outcomes, and a plain success for every rung the walk never
reached. A success is the filler because it is the one outcome that ends
`evaluate`'s loop with a winner, so a policy that runs one rung too far reports
a longer `ran` and a different `winner` instead of quietly agreeing.
"""

from itertools import product

import pytest

from tests import _ladder_fsm_model as model
from tests._resolver_helpers import load_script

ladder = load_script(".github/resolver/auto-resolve/_ladder.py")
run_ladder = load_script(".github/resolver/auto-resolve/run-ladder.py")

FILLER = ladder.RungOutcome(errored=False, zero_cost=False)


def outcome_of(symbol: str) -> object:
    """The `RungOutcome` a model symbol stands for — the first flag combination
    `symbol_of` maps onto it, so this inverts the model's own collapse rather
    than restating it."""
    flags = next(f for f in model.ALL_FLAGS if model.symbol_of(*f) == symbol)
    errored, zero_cost, wall_clock_only = flags
    return ladder.RungOutcome(
        errored=errored, zero_cost=zero_cost, wall_clock_only=wall_clock_only
    )


def rungs_for(state, monkeypatch) -> list:
    """The rung list this configuration really produces, straight from
    `_slots()` and mapped the way `run-ladder.py`'s `main()` maps it."""
    for i in model.RUNGS:
        configured = i == "1" or getattr(state, f"configured{i}")
        monkeypatch.setenv(f"RUNG_{i}_TOKEN", "token" if configured else "")
    return [
        ladder.Rung(
            name=slot.name, token_env=slot.spec.env_var, configured=slot.configured
        )
        for slot in run_ladder._slots()
    ]


def outcomes_for(state) -> dict:
    return {
        f"rung_{i}": FILLER
        if getattr(state, f"o{i}") == "NOT_RUN"
        else outcome_of(getattr(state, f"o{i}"))
        for i in model.RUNGS
    }


def configurations() -> list[tuple[bool, ...]]:
    """Every CONFIGURED combination over the rungs past the first."""
    slots = len(model.RUNGS) - 1
    return [
        tuple(bool(combo >> bit & 1) for bit in range(slots))
        for combo in range(2**slots)
    ]


TERMINAL: list = sorted(
    {
        state
        for flags in configurations()
        for state in model.reachable(model.start(*flags))
        if state.pos == "DONE"
    }
)


def verdicts(monkeypatch):
    """Every terminal walk paired with what the shipped ladder decides for it."""
    for state in TERMINAL:
        yield state, ladder.evaluate(rungs_for(state, monkeypatch), outcomes_for(state))


def test_the_reachable_set_is_not_empty_so_this_comparison_is_not_vacuous():
    # A start state with no successors would make every assertion below pass
    # over nothing, which is the failure this whole file exists to catch.
    assert len(TERMINAL) > 1000, len(TERMINAL)


def test_every_outcome_the_decider_can_report_maps_onto_a_modelled_symbol():
    """`claude-run-errored.sh` computes errored, zero_cost and wall_clock_only
    from three independent `jq` tests, so all eight combinations are emittable.
    A combination outside the model is a case no theorem covers.

    The product is built HERE rather than read from `model.ALL_FLAGS`: a model
    that narrowed its own flag set would shrink the loop with it, and this test
    would pass over the cases the narrowing dropped — which is exactly the shape
    of the two defects it exists to catch.
    """
    emittable = set(product((False, True), repeat=3))
    assert set(model.ALL_FLAGS) == emittable, "the model narrowed the flag space"
    for flags in sorted(emittable):
        assert model.symbol_of(*flags) in model.SYMBOLS, flags


def test_the_model_walks_the_rungs_the_shipped_table_declares():
    # The rung count is read from lib_credential_ladder, so this pins the read
    # rather than a number: a rung added there must reach the model.
    assert model.RUNGS == tuple(str(spec.index) for spec in run_ladder.ladder_slots())


def test_the_policy_stops_where_the_model_stops(monkeypatch):
    for state, verdict in verdicts(monkeypatch):
        want = tuple(f"rung_{i}" for i in model.ran(state))
        assert verdict.ran == want, state


def test_the_policy_names_the_winner_the_model_names(monkeypatch):
    for state, verdict in verdicts(monkeypatch):
        want = None if state.winner == "NONE" else f"rung_{state.winner}"
        assert verdict.winner == want, state


def test_the_policy_releases_the_mark_exactly_when_the_model_does(monkeypatch):
    for state, verdict in verdicts(monkeypatch):
        assert verdict.release_attempt is model.released(state), state


def test_a_winner_always_names_a_credential_that_was_actually_set(monkeypatch):
    """`preferred_token_env` is outside the FSM, so it gets its own claim: the
    winning rung's own secret, or its predecessor's when the winner is the free
    retry running on a credential it does not have."""
    for state, verdict in verdicts(monkeypatch):
        if verdict.winner is None:
            assert verdict.preferred_token_env == ""
            continue
        rungs = rungs_for(state, monkeypatch)
        index = next(i for i, r in enumerate(rungs) if r.name == verdict.winner)
        want = (
            rungs[index].token_env
            if index == 0 or rungs[index].configured
            else rungs[index - 1].token_env
        )
        assert verdict.preferred_token_env == want, state
        assert verdict.preferred_token_env != "", state


@pytest.mark.parametrize("gap", [("3",), ("3", "4"), ("4", "5", "6")])
def test_an_error_steps_over_an_unset_rung_to_the_credential_behind_it(
    gap, monkeypatch
):
    """The shipped `_slots()` drops an unconfigured rung, so the walk reaches
    the configured rungs behind a gap. A model that stopped at the gap would
    pass every other test in this file."""
    flags = tuple(i not in gap for i in model.RUNGS[1:])
    state = model.start(*flags)
    rungs = rungs_for(state, monkeypatch)
    assert [r.name for r in rungs] == [f"rung_{i}" for i in model.RUNGS if i not in gap]
    reached = {i for walk in model.reachable(state) for i in model.ran(walk)}
    assert reached.isdisjoint(gap), reached
    assert reached & {i for i in model.RUNGS[3:] if i not in gap}
