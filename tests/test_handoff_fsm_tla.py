"""`docs/tla/Handoff.tla` is what the emitter prints right now.

PROBLEM CLASS — a generated file edited by hand, or left stale by a model edit,
still parses and still model-checks. TLC then proves theorems about a machine the
Python side no longer runs, and both halves report green.
"""

from tests import _handoff_fsm_model as head
from tests import _handoff_fsm_tla as emitter
from tests._helpers import REPO_ROOT

MODULE = REPO_ROOT / emitter.MODULE_PATH


def test_the_committed_module_matches_the_emitters_output():
    assert MODULE.read_text(encoding="utf-8") == emitter.module_text(), (
        f"{emitter.MODULE_PATH} is stale — regenerate with"
        " `uv run python -m tests._handoff_fsm_tla`"
    )


def test_every_transition_the_model_compiles_is_emitted_as_an_action():
    # The two notations come from one table, so a transition the interpreter runs
    # and the emitter drops would leave TLC checking less than Python does — and
    # nothing about the file's syntax would say so.
    text = MODULE.read_text(encoding="utf-8")
    assert head.HANDOFF_TRANSITIONS
    for spec in head.HANDOFF_TRANSITIONS:
        assert f"\n{emitter._op_name(spec.name)} ==\n" in text, spec.name


def test_the_theorems_fault_set_is_exactly_the_endings_the_tree_did_not_cause():
    """The fault set must come from `TREE_CAUSED` and never from `marks_head`: a
    set read off the mark rule makes FaultNeverStrandsTheHead say "the rule does
    not mark what it does not mark", which TLC proves of any rule at all. An
    EQUALITY rather than a subset, so no ending leaves the theorem silently."""
    assert set(emitter._fault_causes()) == set(head.ENDINGS) - head.TREE_CAUSED
    # Proper and non-empty both ways, so the theorem quantifies over something
    # and leaves something out. TLC reports the AGREEMENT between the fault set
    # and the mark rule; asserting it here too would prove it from Python.
    assert emitter._fault_causes() and set(emitter._fault_causes()) < set(head.ENDINGS)


def test_the_reachable_set_is_the_size_the_config_declares():
    # `Handoff_safety.cfg` pins EXPECT-DISTINCT, so a model edit that silently
    # moves the reachable set reds in CI. This is the same count from the Python
    # side, which is what tells a stale config from a real model change.
    assert len(head.reachable()) == 13
