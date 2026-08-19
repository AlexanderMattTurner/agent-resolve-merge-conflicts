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


def test_every_land_ending_the_gate_knows_is_sorted_into_one_of_the_theorem_sets():
    # ConflictStandsImpliesStall's antecedent excludes the endings that resolve
    # the conflict or hand it to somebody else. An ending added to `outcome.Land`
    # and left out of both sets would silently widen the antecedent instead of
    # being judged, so the emitter's two lists must cover exactly what they claim.
    named = set(emitter._RESOLVED_LANDS) | set(emitter._HANDED_ON_LANDS)
    assert named <= set(emitter.run.LANDS)
    for land in named:
        facts = emitter.run.outcome.RunFacts(
            selected=True,
            claim=emitter.run.outcome.Claim.OWNED,
            published=emitter.run.outcome.Published.NONE,
            land=emitter.run.outcome.Land(land.lower()),
        )
        assert not emitter.run.outcome.verdict(facts).stall, land
