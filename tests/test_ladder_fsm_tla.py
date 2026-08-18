"""`docs/tla/Ladder.tla` is what the emitter prints right now.

PROBLEM CLASS — a generated file edited by hand, or left stale by a model edit,
still parses and still model-checks. TLC then proves theorems about a machine
the Python side no longer runs, and both halves report green.

The check is the whole file, not a marker region: the module is generated end to
end, and `.gitattributes` marks it so a merge re-derives it instead of line-
merging two versions of a table neither side wrote.
"""

from tests import _ladder_fsm_tla as emitter
from tests._helpers import REPO_ROOT

MODULE = REPO_ROOT / emitter.MODULE_PATH


def test_the_committed_module_matches_the_emitters_output():
    assert MODULE.read_text(encoding="utf-8") == emitter.module_text(), (
        f"{emitter.MODULE_PATH} is stale — regenerate with"
        " `uv run python -m tests._ladder_fsm_tla`"
    )


def test_every_config_beside_it_names_a_module_that_exists():
    # A config whose stem matches no module is one TLC would refuse to run, and
    # the run reports that as a missing file rather than as an unchecked theorem.
    modules = {p.stem for p in MODULE.parent.glob("*.tla")}
    for cfg in sorted(MODULE.parent.glob("*.cfg")):
        assert any(cfg.stem == m or cfg.stem.startswith(f"{m}_") for m in modules), cfg


def test_every_transition_the_model_compiles_is_emitted_as_an_action():
    # The two notations are derived from one table, so a transition the
    # interpreter runs and the emitter drops would leave TLC checking less than
    # Python does — and nothing about the file's syntax would say so.
    text = MODULE.read_text(encoding="utf-8")
    for spec in emitter.ladder.LADDER_TRANSITIONS:
        assert f"\n{emitter._op_name(spec.name)} ==\n" in text, spec.name
