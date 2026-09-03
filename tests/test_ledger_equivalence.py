"""The FSM in `tests/_ledger_fsm_model.py` decides every claim exactly as the
shipped ledger does.

PROBLEM CLASS — a claim rule re-derived as a state machine drifts from the code
it models the moment either side changes alone. `docs/tla/ConflictLedger.tla` is
generated from that state machine, so every theorem TLC proves there is a
theorem about the shipped ledger only while this comparison holds.

The comparison drives the real `ConflictSet.claim` over every ENTRY the model
can reach, crossed with every pass and every claim a pass can make. Both
directions are checked: the ledger refuses exactly the claims the model has no
transition for, and it records exactly the entry the model's successor holds.

The model's `handed` and `ran_<pass>` fields are left out of the comparison.
They are history the ledger does not keep, recorded only so a TLA+ witness can
name a state the current dispositions alone cannot describe.
"""

# covers: .github/resolver/auto-resolve/_conflict_set.py

from tests import _ledger_fsm_model as model
from tests._conflict_ledger import conflict_set, paths as paths_module

Claimed = conflict_set.Claimed
Disposition = conflict_set.Disposition

PATH = "src/app.py"
# One conflicted path is enough: `claim` reads and writes the entry it names and
# nothing else, and the model states that too — every transition guards on one
# path's fields and updates only those.
STAGES = conflict_set.Stages(base="0" * 40, ours="1" * 40, theirs="2" * 40)
# Built rather than classified: `claim` reads no field of PathFacts, and asking
# `_paths.classify` for one would need a real conflicted checkout to read them from.
FACTS = paths_module.PathFacts(
    path=PATH,
    shape=STAGES.shape,
    policy=paths_module.MergePolicy.PLAIN,
    binary=False,
    unmergeable=False,
    protected=False,
    harness_unwritable=False,
    generated_owned=False,
    lockfile=False,
)

STATES = model.reachable(model.start())
ENTRIES: list[tuple[str, str, str, str]] = sorted(
    {model.config(state, path) for state in STATES for path in model.PATHS}
)
CLAIMS = [
    (entry, by, target)
    for entry in ENTRIES
    for by in model.PASSES
    for target in model.TARGETS
]


def _disposition(entry: tuple[str, str, str, str]) -> object:
    """The `Disposition` a model entry stands for. A REFUSED entry gets the
    model's one reason, which `claim` never reads and `Disposition` only
    requires to be non-empty."""
    claimed, by, to, prompt = entry
    return Disposition(
        claimed=Claimed(claimed),
        by=by,
        to=to,
        prompt=prompt,
        reason=model.REASON if claimed == str(Claimed.REFUSED) else "",
    )


def _entry_of(disposition) -> tuple[str, str, str, str]:
    return (
        str(disposition.claimed),
        disposition.by,
        disposition.to,
        disposition.prompt,
    )


def _ledger(entry: tuple[str, str, str, str]):
    """A one-path ledger holding ENTRY, built through the shipped classes."""
    return conflict_set.ConflictSet(
        {
            PATH: conflict_set.Entry(
                path=PATH,
                stages=STAGES,
                facts=FACTS,
                disposition=_disposition(entry),
            )
        }
    )


def _shipped(entry, by: str, target) -> tuple[str, str, str, str] | None:
    """The entry the shipped ledger records, or None when it refuses."""
    ledger = _ledger(entry)
    claim = Disposition(
        claimed=Claimed(target.claimed),
        by=by,
        to=target.to,
        prompt=target.prompt,
        reason=target.reason,
    )
    try:
        ledger.claim(PATH, disposition=claim)
    except conflict_set.ClaimConflict:
        return None
    return _entry_of(ledger.entry(PATH).disposition)


def _modelled(entry, by: str, target) -> tuple[str, str, str, str] | None:
    """The entry the model records, or None when no transition is enabled."""
    path = model.PATHS[0]
    state = model.with_config(path, entry)
    wanted = model.claim_names(path, by, target)
    taken = [nxt for name, nxt in model.successors(state) if name in wanted]
    # The two guard variants are "unclaimed" and "deferred to this pass", so at
    # most one can fire. Two firing would make the comparison below pick one.
    assert len(taken) <= 1, (entry, by, target.name)
    return model.config(taken[0], path) if taken else None


def test_the_comparison_covers_every_entry_the_ledger_can_hold():
    """Every `Claimed` member and every `PROMPTS` member appears in the entries
    compared below, so no disposition escapes the comparison."""
    assert len(STATES) > 1000, len(STATES)
    assert {entry[0] for entry in ENTRIES} == {str(member) for member in Claimed}
    assert {entry[3] for entry in ENTRIES if entry[3]} == set(conflict_set.PROMPTS)
    assert {entry[2] for entry in ENTRIES if entry[2]} == set(model.PASSES)


def test_both_answers_occur_so_neither_comparison_passes_over_nothing():
    # A model that enabled everything, or nothing, would agree with a ledger
    # that did the same and prove neither rule.
    answers = [_shipped(*claim) for claim in CLAIMS]
    assert any(answer is None for answer in answers)
    assert any(answer is not None for answer in answers)


def test_the_ledger_refuses_exactly_the_claims_the_model_has_no_transition_for():
    for entry, by, target in CLAIMS:
        allowed = _modelled(entry, by, target) is not None
        assert allowed == (_shipped(entry, by, target) is not None), (
            entry,
            by,
            target.name,
        )


def test_the_ledger_records_exactly_the_entry_the_model_records():
    for entry, by, target in CLAIMS:
        modelled = _modelled(entry, by, target)
        if modelled is None:
            continue
        assert modelled == _shipped(entry, by, target), (entry, by, target.name)
