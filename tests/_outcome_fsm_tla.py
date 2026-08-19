"""Prints `docs/tla/AutoResolve.tla` from the transition table in
`tests/_outcome_fsm_model.py`, so the Python checker and the TLA+ module cannot
drift. Regenerate after a model edit:

    uv run python -m tests._outcome_fsm_tla

The freshness test in `tests/test_outcome_fsm_tla.py` fails when the committed
module differs from this emitter's output.
"""

import subprocess
from pathlib import Path

from tests import _outcome_fsm_model as run
from tests._fsm_tla import (
    Ctx,
    _disjunction,
    _lit,
    _op_name,
    _set,
    _transition,
    normalized,
)

RUN_FIELDS = {f: f for f in run.Run._fields}
MODULE_PATH = Path("docs") / "tla" / "AutoResolve.tla"

# The land endings that put a resolution on the branch, and the ones that name
# somebody else as the party who carries the conflict next. Both are read out of
# the shipped verdicts rather than listed here, so an ending added to
# `outcome.Land` joins the right set or fails the equivalence proof.
_RESOLVED_LANDS = ("PUSHED", "NOT_NEEDED")
_HANDED_ON_LANDS = ("SUPERSEDED", "QUEUE_HELD")


def _theorems() -> str:
    """The completeness claim, and the three witnesses a reader would not predict."""
    stalls = _set(sorted(run.STALLS))
    resolved = _set(_RESOLVED_LANDS)
    handed_on = _set(_HANDED_ON_LANDS)
    return f"""\\* Every field stays within its declared domain -- a structural check on the
\\* generated updates.
TypeOK == s \\in AllStates

\\* The verdicts that mean the conflict is still there and nothing else carries
\\* it. outcome.py's `stall` flag decides the membership, so this set cannot
\\* disagree with the exit status the gate reports.
Stall == s.verdict \\in {stalls}

\\* Totality: a run that ended carries a verdict.  Without this an enum member
\\* added with no arm would end a run classified as NONE, which the gate reads as
\\* neither a stall nor a success.
TerminalHasVerdict == s.phase = "DONE" => s.verdict # "NONE"

\\* THE CLAIM THIS MODULE EXISTS FOR -- a run that resolves nothing must not
\\* report success.  Read the antecedent as the four facts that together mean the
\\* conflict is still there and nobody else is on the hook for it: this run took
\\* the pull request on, no other run holds the head, there was a merge to make,
\\* and the land job neither pushed nor handed the head to a later run.  Every
\\* such ending has to be a stall, which is what the gate exits non-zero on.
ConflictStandsImpliesStall ==
    (   /\\ s.phase = "DONE"
        /\\ s.selected
        /\\ s.claim # "DUPLICATE"
        /\\ s.published # "NO_OP"
        /\\ s.land \\notin ({resolved} \\union {handed_on})
    ) => Stall

\\* A landed resolution is never a stall.  The two sets are defined apart, so
\\* this is what stops a future edit putting an ending in both.
LandedIsNotStall == s.verdict = "landed" => ~Stall

Inv ==
    /\\ TypeOK
    /\\ TerminalHasVerdict
    /\\ ConflictStandsImpliesStall
    /\\ LandedIsNotStall

\\* A verdict is written once, by the transition that ends the run, and no later
\\* step rewrites it.
VerdictStable == [][ s.verdict # "NONE" => s'.verdict = s.verdict ]_s

\\* Non-vacuity witnesses (EXPECT-EXIT 12 in the .cfg): TLC's counterexample
\\* trace over the negation IS the reachability proof.

\\* A head latched by an attempt mark whose run cannot be identified is reachable,
\\* and it is a stall.  This is the ending that used to report success: every step
\\* after the stand-down skipped, and the run concluded green having landed
\\* nothing.
NoLatchedStall == ~(s.verdict = "latched")

\\* A paid run that asks a human to resolve the conflict is a stall too.  It
\\* published a verdict, so the pull request says something -- but the conflict is
\\* still there, and a green run reaches no failure route.
NoHandedOffStall == ~(s.verdict = "handed_off")

\\* And the converse a reader would not predict from the claim above: a run can
\\* end WITHOUT pushing and still report success, when the ending names who
\\* carries the conflict instead -- another live run, a fresh run already
\\* dispatched, or the merge queue.
NoGreenWithoutPush ==
    ~(s.phase = "DONE" /\\ ~Stall /\\ s.land # "PUSHED")

=============================================================================
"""


def emit_run() -> str:
    c = Ctx("s", RUN_FIELDS)
    domains = []
    for field in run.Run._fields:
        dom = run.FIELD_DOMAINS[field]
        text = "BOOLEAN" if dom == (False, True) else _set(dom)
        domains.append(f"    {c.fields[field]}: {text}")
    domain_rows = ",\n".join(domains)
    init_row = ", ".join(
        f"{f} |-> {_lit(getattr(run.START, f))}" for f in run.Run._fields
    )
    transitions = "\n\n".join(_transition(c, sp) for sp in run.OUTCOME_TRANSITIONS)
    next_ops = _disjunction(
        "Next", [_op_name(sp.name) for sp in run.OUTCOME_TRANSITIONS]
    )
    return f"""----------------------------- MODULE AutoResolve -----------------------------
(* GENERATED FILE — do not edit. tests/_outcome_fsm_tla.py prints this      *)
(* module from the transition table in tests/_outcome_fsm_model.py;         *)
(* regenerate with `uv run python -m tests._outcome_fsm_tla`.               *)
(***************************************************************************)
(* ONE auto-resolve run, from the pull request it was dispatched for to the *)
(* verdict its outcome gate reports.  `phase` walks SELECT, CLAIM, RESOLVE, *)
(* LAND and then DONE -- the only terminal marker, since every action       *)
(* strictly advances it.  A run can stop at any phase, so each phase has a  *)
(* transition that ends the run and writes its verdict.                     *)
(* Python twin: tests/_outcome_fsm_model.py, proved against the shipped     *)
(* rule in .github/resolver/auto-resolve/outcome.py by                      *)
(* tests/test_outcome_equivalence.py.                                       *)
(***************************************************************************)

VARIABLE s

AllStates == [
{domain_rows}
]

Init == s = [{init_row}]

{transitions}

{next_ops}
"""


def module_text() -> str:
    """The committed module: the generated transition table, then the theorems
    the configs name."""
    return normalized(emit_run() + "\n" + _theorems())


def main() -> None:
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    path = root / MODULE_PATH
    path.write_text(module_text(), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
