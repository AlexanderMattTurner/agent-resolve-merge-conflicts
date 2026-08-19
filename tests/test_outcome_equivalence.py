"""The FSM in `tests/_outcome_fsm_model.py` classifies every run exactly as the
shipped outcome gate does.

PROBLEM CLASS — a policy re-derived as a state machine drifts from the code it
models the moment either side changes alone. `docs/tla/AutoResolve.tla` is
generated from that state machine, so every theorem TLC proves there is a theorem
about the shipped gate only while this comparison holds.

The comparison also drives the gate's PROCESS boundary, because the exit status
is what routes a stall to the failure notifier: a verdict the function calls a
stall and the process reports 0 for would leave the conflict standing on a green
run, which is the defect the gate exists to remove.
"""

import os
import subprocess
import sys

import pytest

from tests import _outcome_fsm_model as model
from tests._helpers import REPO_ROOT

# covers: .github/resolver/auto-resolve/outcome.py
GATE = REPO_ROOT / ".github" / "resolver" / "auto-resolve" / "outcome.py"

outcome = model.outcome

EVERY_FACT = [
    outcome.RunFacts(selected, claim, published, land)
    for selected in (False, True)
    for claim in outcome.Claim
    for published in outcome.Published
    for land in outcome.Land
]


def _run_gate(facts: outcome.RunFacts) -> subprocess.CompletedProcess[str]:
    """The real gate as the workflow runs it: one python3 process, the facts in
    its environment, and the exit status it leaves behind."""
    return subprocess.run(
        [sys.executable, str(GATE)],
        env={
            **os.environ,
            "SELECTED": "true" if facts.selected else "false",
            "CLAIM": facts.claim.value,
            "PUBLISHED": facts.published.value,
            "LAND": facts.land.value,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_fact_set_is_not_empty():
    # Every case below is driven from this list, so an enum that stopped
    # enumerating would turn the whole file green over nothing.
    assert len(EVERY_FACT) == 2 * 4 * 4 * 7


@pytest.mark.parametrize(
    "facts",
    EVERY_FACT,
    ids=lambda f: f"{f.claim.value}-{f.published.value}-{f.land.value}-{f.selected}",
)
def test_every_combination_of_facts_reaches_one_named_verdict(facts):
    # Totality over the whole product, not over the reachable set: an enum member
    # added with no arm falls off the end of `verdict` rather than reaching a
    # default, and a run the gate cannot classify is one it cannot judge.
    found = outcome.verdict(facts)
    assert found.name
    assert found.sentence.endswith(".")


def test_the_model_and_the_shipped_gate_agree_on_every_reachable_run():
    for state in model.reachable():
        if state.phase != "DONE":
            continue
        facts = outcome.RunFacts(
            selected=state.selected,
            claim=outcome.Claim(state.claim.lower()),
            published=outcome.Published(state.published.lower()),
            land=outcome.Land(state.land.lower()),
        )
        assert state.verdict == outcome.verdict(facts).name, state


def test_a_stall_exits_non_zero_and_names_itself_as_an_error():
    # The stand-down on an unidentifiable claim: the ending that used to conclude
    # success with every later step skipped.
    latched = outcome.RunFacts(
        True, outcome.Claim.LATCHED, outcome.Published.NONE, outcome.Land.NOT_RUN
    )
    done = _run_gate(latched)
    assert done.returncode == 1
    assert "::error::auto-resolve outcome: latched" in done.stderr


def test_a_handoff_verdict_reds_the_run():
    # A paid run that asks a human is still a run that resolved nothing, so it
    # must reach the failure route rather than only a pull-request comment.
    facts = outcome.RunFacts(
        True, outcome.Claim.OWNED, outcome.Published.HANDOFF, outcome.Land.NO_BUNDLE
    )
    done = _run_gate(facts)
    assert done.returncode == 1
    assert "handed_off" in done.stderr


@pytest.mark.parametrize(
    "facts",
    [
        outcome.RunFacts(
            False, outcome.Claim.NONE, outcome.Published.NONE, outcome.Land.NOT_RUN
        ),
        outcome.RunFacts(
            True, outcome.Claim.DUPLICATE, outcome.Published.NONE, outcome.Land.NOT_RUN
        ),
        outcome.RunFacts(
            True, outcome.Claim.OWNED, outcome.Published.NONE, outcome.Land.PUSHED
        ),
        outcome.RunFacts(
            True, outcome.Claim.OWNED, outcome.Published.NONE, outcome.Land.QUEUE_HELD
        ),
        outcome.RunFacts(
            True, outcome.Claim.OWNED, outcome.Published.NONE, outcome.Land.SUPERSEDED
        ),
    ],
    ids=["refused", "duplicate", "landed", "held", "superseded"],
)
def test_an_ending_that_names_who_carries_the_conflict_exits_zero(facts):
    done = _run_gate(facts)
    assert done.returncode == 0
    assert "::error::" not in done.stderr


def test_an_unset_environment_reads_as_a_run_that_got_nowhere(monkeypatch):
    # A job that died before it wrote any fact must not read as one that
    # succeeded, so every default is the never-reached member.
    for name in ("SELECTED", "CLAIM", "PUBLISHED", "LAND"):
        monkeypatch.delenv(name, raising=False)
    assert outcome.RunFacts.from_env() == outcome.RunFacts(
        selected=False,
        claim=outcome.Claim.NONE,
        published=outcome.Published.NONE,
        land=outcome.Land.NOT_RUN,
    )
