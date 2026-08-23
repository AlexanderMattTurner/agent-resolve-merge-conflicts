"""Prints `docs/tla/Ladder.tla` from the transition table in
`tests/_ladder_fsm_model.py`, so the Python checker and the TLA+ module cannot
drift. Regenerate after a model edit:

    uv run python -m tests._ladder_fsm_tla

The freshness test in `tests/test_ladder_fsm_tla.py` fails when the committed
module differs from this emitter's output.
"""

from pathlib import Path

from tests import _ladder_fsm_model as ladder
from tests._fsm_tla import (
    Ctx,
    _conjunction,
    _disjunction,
    _op_name,
    _set,
    _transition,
    normalized,
    write_module,
)


LADDER_FIELDS = {f: f for f in ladder.Lg._fields}
MODULE_PATH = Path("docs") / "tla" / "Ladder.tla"


def _init_record() -> str:
    """The initial-state set: nothing run, every CONFIGURED combination.

    Generated rather than written out, so a rung added to the credential table
    appears here without anyone remembering to widen a literal record.
    """
    flags = ladder.RUNGS[1:]
    outcomes = ",\n     ".join(
        ", ".join(f'o{i} |-> "NOT_RUN"' for i in ladder.RUNGS[n : n + 4])
        for n in range(0, len(ladder.RUNGS), 4)
    )
    bindings = ",\n     ".join(
        ", ".join(f"configured{i} |-> c{i}" for i in flags[n : n + 3])
        for n in range(0, len(flags), 3)
    )
    quantifier = ",\n    ".join(
        ", ".join(f"c{i} \\in BOOLEAN" for i in flags[n : n + 4])
        for n in range(0, len(flags), 4)
    )
    return f"""Inits == {{
    [pos |-> "1",
     {outcomes},
     winner |-> "NONE",
     {bindings}] :
    {quantifier}
}}"""


def _theorems() -> str:
    """The safety scheme and the non-vacuity witnesses, over whatever rungs and
    outcome symbols the credential table and the outcome flags produce."""
    rungs = ladder.RUNGS
    zero_cost = sorted(ladder.ZERO_COST_OUTCOMES)
    wall = sorted(s for s in ladder.SYMBOLS if s.startswith("ERR_WALL"))
    free_retry, gap, behind = rungs[1], rungs[2], rungs[3]

    all_zero = _conjunction(
        "AllZeroCost",
        [f"s.o{i} \\in {_set(['NOT_RUN', *zero_cost])}" for i in rungs],
    )
    any_ran = "AnyRan ==\n" + "\n".join(f'    \\/ s.o{i} # "NOT_RUN"' for i in rungs)
    ran_configured = _conjunction(
        "RungRanRequiresConfigured",
        [f'(s.o{i} # "NOT_RUN" => s.configured{i})' for i in rungs[2:]],
    )
    no_wall_advance = _conjunction(
        "NoAdvanceFromWall",
        [f'(s.o{i} \\in {_set(wall)} => s.pos = "DONE")' for i in rungs],
    )
    one_attempt = _conjunction(
        "AtMostOneAttempt",
        [f'[][ s.o{i} # "NOT_RUN" => s\'.o{i} = s.o{i} ]_s' for i in rungs],
    )
    paid_walk = (
        "NoFullPaidWalk ==\n    ~("
        + "\n      ".join(
            [
                "/\\ " + " /\\ ".join(f's.o{i} = "ERR_PAID"' for i in rungs[n : n + 3])
                for n in range(0, len(rungs), 3)
            ]
        )
        + '\n      /\\ s.pos = "DONE")'
    )

    return f"""\\* Every field stays within its declared domain -- a structural check on the
\\* generated updates, not a restatement of anything Python already proves.
TypeOK == s \\in AllStates

\\* `evaluate`'s release rule: at least one rung ran, and every rung that ran
\\* BILLED NOTHING.  The rule reads zero_cost alone and never errored, so a
\\* zero-billed success and a zero-billed wall-clock failure both count here.
{all_zero}

{any_ran}

Released == AnyRan /\\ AllZeroCost

\\* The walk never continues past a winner.
WinnerImpliesDone == s.winner # "NONE" => s.pos = "DONE"

\\* No rung past the free-retry boundary runs without its OWN credential.
{ran_configured}

\\* Rung {free_retry} without its own credential is reached only via the free retry, and
\\* only from a zero-cost error: a wall-clock failure never advances.
Rung{free_retry}NeedsConfiguredOrFreeRetry ==
    (s.o{free_retry} # "NOT_RUN" /\\ ~s.configured{free_retry}) => s.o1 = "ERR_ZERO"

\\* A wall-clock-only failure never advances, at ANY rung -- unlike ERR_PAID,
\\* which steps on whenever a later rung holds its own credential.  A fresh
\\* credential faces the identical wall.
{no_wall_advance}

Inv ==
    /\\ TypeOK
    /\\ WinnerImpliesDone
    /\\ RungRanRequiresConfigured
    /\\ Rung{free_retry}NeedsConfiguredOrFreeRetry
    /\\ NoAdvanceFromWall

\\* No release invariant sits in Inv: `Released` is DEFINED from the outcomes, so
\\* every predicate written over it here is true of any model and proves nothing.
\\* What the release rule is held to is the equivalence proof against `evaluate`
\\* in tests/test_ladder_equivalence.py.  The two witnesses below are what this
\\* module can say about it -- both of them surprising, and both real.

\\* At most one attempt per rung: once an outcome is recorded, no later step
\\* changes it -- the walk never revisits a rung it already ran.
{one_attempt}

\\* Winner uniqueness: once chosen, the winner never changes.
WinnerStable == [][ s.winner # "NONE" => s'.winner = s.winner ]_s

\\* Non-vacuity witnesses (EXPECT-EXIT 12 in the .cfg): TLC's counterexample
\\* trace over the negation IS the reachability proof.
NoFreeRetry == ~(s.o1 = "ERR_ZERO" /\\ ~s.configured{free_retry} /\\ s.o{free_retry} # "NOT_RUN")

{paid_walk}

\\* A wall-clock-only failure at rung {gap} ends the walk even though rung {behind} has
\\* its OWN distinct credential configured -- the stop is the wall's doing, not
\\* an absent credential's, which is what tells it apart from ERR_PAID.
NoWallDespiteConfigured ==
    ~(s.o{gap} = "ERR_WALL" /\\ s.configured{behind} /\\ s.pos = "DONE")

\\* run-ladder.py's _slots() drops an unconfigured rung before the walk, so an
\\* error at rung {free_retry} steps OVER the gap to the next rung that HAS its own
\\* credential.  A model that stopped at the gap would describe a ladder that
\\* never reaches the credentials behind it.
NoSkipOverGap ==
    ~(s.o{free_retry} # "NOT_RUN" /\\ ~s.configured{gap} /\\ s.o{behind} # "NOT_RUN")

\\* A zero-billed success both names a winner and hands the attempt mark back.
NoReleasedWinner == ~(Released /\\ s.winner # "NONE")

\\* A wall-clock-only failure that billed nothing hands the mark back too:
\\* claude-run-errored.sh computes zero_cost and wall_clock_only from separate
\\* tests, so a shard that died at the wall having reached no inference sets both.
NoReleasedWall == ~(Released /\\ s.o1 = "ERR_WALL_ZERO")

=============================================================================
"""


def emit_ladder() -> str:
    c = Ctx("s", LADDER_FIELDS)
    domains = []
    for f in ladder.Lg._fields:
        dom = ladder.FIELD_DOMAINS[f]
        text = "BOOLEAN" if dom == (False, True) else _set(dom)
        domains.append(f"    {c.fields[f]}: {text}")
    domain_rows = ",\n".join(domains)
    transitions = "\n\n".join(_transition(c, sp) for sp in ladder.LADDER_TRANSITIONS)
    next_ops = _disjunction(
        "Next", [_op_name(sp.name) for sp in ladder.LADDER_TRANSITIONS]
    )
    return f"""------------------------------- MODULE Ladder -------------------------------
(* GENERATED FILE — do not edit. tests/_ladder_fsm_tla.py prints this       *)
(* module from the transition table in tests/_ladder_fsm_model.py;          *)
(* regenerate with `uv run python -m tests._ladder_fsm_tla`.                *)
(***************************************************************************)
(* The credential ladder's retry policy: the credential rungs of           *)
(* lib_credential_ladder.py's table, walked in order.  `pos` is the rung    *)
(* about to run, or "DONE" once the walk stops -- the only terminal marker, *)
(* since every action strictly advances it.  Rung 1's zero-cost error       *)
(* always advances (the same-token free retry); every later boundary needs  *)
(* its OWN configured credential, and run-ladder.py drops an unconfigured   *)
(* rung entirely, so an error steps OVER a gap rather than stopping at it.  *)
(* Python twin: tests/_ladder_fsm_model.py, proved against the shipped      *)
(* policy by tests/test_ladder_equivalence.py.                              *)
(***************************************************************************)

VARIABLE s

AllStates == [
{domain_rows}
]

\\* Nothing run, over every CONFIGURED combination.
{_init_record()}

Init == s \\in Inits

{transitions}

{next_ops}
"""


def module_text() -> str:
    """The committed module: the generated transition table, then the theorems
    the configs name."""
    return normalized(emit_ladder() + "\n" + _theorems())


def main() -> None:
    write_module(MODULE_PATH, module_text())


if __name__ == "__main__":
    main()
