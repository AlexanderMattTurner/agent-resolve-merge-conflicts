"""`docs/tla/AutoResolve.tla` is what the emitter prints right now.

PROBLEM CLASS — a generated file edited by hand, or left stale by a model edit,
still parses and still model-checks. TLC then proves theorems about a machine the
Python side no longer runs, and both halves report green.

The check is the whole file, not a marker region: the module is generated end to
end from the transition table.
"""

from tests import _outcome_fsm_tla as emitter
from tests._helpers import REPO_ROOT

MODULE = REPO_ROOT / emitter.MODULE_PATH


def test_the_committed_module_matches_the_emitters_output():
    assert MODULE.read_text(encoding="utf-8") == emitter.module_text(), (
        f"{emitter.MODULE_PATH} is stale — regenerate with"
        " `uv run python -m tests._outcome_fsm_tla`"
    )


def test_every_transition_the_model_compiles_is_emitted_as_an_action():
    # The two notations come from one table, so a transition the interpreter runs
    # and the emitter drops would leave TLC checking less than Python does — and
    # nothing about the file's syntax would say so.
    text = MODULE.read_text(encoding="utf-8")
    assert emitter.run.OUTCOME_TRANSITIONS
    for spec in emitter.run.OUTCOME_TRANSITIONS:
        assert f"\n{emitter._op_name(spec.name)} ==\n" in text, spec.name


def test_the_theorems_land_set_is_exactly_the_endings_that_settle_the_conflict():
    # ConflictStandsImpliesStall's antecedent excludes these endings, so the set
    # has to be an EQUALITY with the non-stall endings, not a subset of them. A
    # `Land` member that settles the conflict and is missing here would widen the
    # antecedent and make the theorem claim more than the gate does; one that does
    # NOT settle it and is present here would narrow the theorem to nothing.
    settled = set(emitter._settled_lands())
    assert settled == {
        land
        for land in emitter.run.LANDS
        if emitter.run.verdict_of(True, "OWNED", "NONE", land) not in emitter.run.STALLS
    }
    assert settled and settled < set(emitter.run.LANDS)
