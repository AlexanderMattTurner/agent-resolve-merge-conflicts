"""Prints `docs/tla/Handoff.tla` from the transition table in
`tests/_handoff_fsm_model.py`, so the Python checker and the TLA+ module cannot
drift. Regenerate after a model edit:

    uv run python -m tests._handoff_fsm_tla

The freshness test in `tests/test_handoff_fsm_tla.py` fails when the committed
module differs from this emitter's output.
"""

import subprocess
from pathlib import Path

from tests import _handoff_fsm_model as head
from tests._fsm_tla import (
    Ctx,
    _disjunction,
    _lit,
    _op_name,
    _set,
    _transition,
    normalized,
)

HEAD_FIELDS = {f: f for f in head.Head._fields}
MODULE_PATH = Path("docs") / "tla" / "Handoff.tla"


def _fault_causes() -> tuple[str, ...]:
    """The endings that must never strand the head: every one the TREE did not
    cause, so a re-run against the same head can answer differently.

    Read from `TREE_CAUSED`, never from `marks_head`. Deriving it from the mark
    rule would make the theorem say "the rule does not mark what it does not
    mark" — true of any rule, including one that marks a plumbing fault.
    """
    return tuple(cause for cause in head.ENDINGS if cause not in head.TREE_CAUSED)


def _theorems() -> str:
    """The claim this module exists for, and the converse witness."""
    faults = " \\/ ".join(f's.cause = "{c}"' for c in _fault_causes())
    latching = _set(c for c in head.ENDINGS if c in head.TREE_CAUSED)
    return f"""\\* Every field stays within its declared domain -- a structural check on the
\\* generated updates.
TypeOK == s \\in AllStates

\\* The second run reads the MARK, never the cause.  A retry that stood down
\\* without one would be a stand-down nothing recorded, so this pins the retry
\\* transitions to the only fact `discover` can actually see.
StandDownRequiresAMark == s.retry = "STOOD_DOWN" => s.marked

\\* THE CLAIM THIS MODULE EXISTS FOR -- a run that failed for a reason the TREE
\\* did not cause never strands the head.  The mark exists to stop the resolver
\\* paying an LLM again for an answer whose inputs did not change; a binary this
\\* job never installed is fixed OUTSIDE the pull request, so a re-run against
\\* the same head answers differently and the mark would only cost the TTL.
\\*
\\* The two sides come from different declarations and that is what makes this a
\\* theorem: the causes below are the ones `TREE_CAUSED` does NOT list, while
\\* `marked` comes from the mark rule.  A rule that grew to mark a plumbing fault
\\* reds here, and every call site's own test still passes.
FaultNeverStrandsTheHead ==
    ~( ({faults}) /\\ s.retry = "STOOD_DOWN" )

Inv ==
    /\\ TypeOK
    /\\ StandDownRequiresAMark
    /\\ FaultNeverStrandsTheHead

\\* The mark is written once, by the transition after the run ends, and no later
\\* step takes it back.
MarkIsStable == [][ s.marked => s'.marked ]_s

\\* Non-vacuity witness (EXPECT-EXIT 12 in the .cfg): TLC's counterexample trace
\\* over the negation IS the reachability proof.

\\* The converse the claim above does not give, and the reason it is not
\\* satisfied by never marking anything: a run the MERGE beat does still latch the
\\* next one, which is the whole purpose of the mark.
NoLatchingEndingAtAll ==
    ~(s.cause \\in {latching} /\\ s.retry = "STOOD_DOWN")

=============================================================================
"""


def emit_head() -> str:
    c = Ctx("s", HEAD_FIELDS)
    domains = []
    for field in head.Head._fields:
        dom = head.FIELD_DOMAINS[field]
        text = "BOOLEAN" if dom == (False, True) else _set(dom)
        domains.append(f"    {c.fields[field]}: {text}")
    domain_rows = ",\n".join(domains)
    init_row = ", ".join(
        f"{f} |-> {_lit(getattr(head.START, f))}" for f in head.Head._fields
    )
    transitions = "\n\n".join(_transition(c, sp) for sp in head.HANDOFF_TRANSITIONS)
    next_ops = _disjunction(
        "Next", [_op_name(sp.name) for sp in head.HANDOFF_TRANSITIONS]
    )
    return f"""------------------------------- MODULE Handoff -------------------------------
(* GENERATED FILE — do not edit. tests/_handoff_fsm_tla.py prints this      *)
(* module from the transition table in tests/_handoff_fsm_model.py;         *)
(* regenerate with `uv run python -m tests._handoff_fsm_tla`.               *)
(***************************************************************************)
(* ONE head across TWO auto-resolve runs.  `phase` walks RUN1, MARK, RETRY  *)
(* and then DONE -- the only terminal marker, since every action strictly   *)
(* advances it.  `cause` is how the first run ended, `marked` whether that  *)
(* wrote the handoff attempt mark, and `retry` what the second run did.     *)
(* The single-run module beside this one ends at a verdict; this one starts *)
(* there, because a wrongly marked head costs the NEXT run, not this one.   *)
(* Python twin: tests/_handoff_fsm_model.py, proved against the shipped     *)
(* rule in .github/resolver/auto-resolve/_refusal.py by                     *)
(* tests/test_handoff_equivalence.py.                                       *)
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
    return normalized(emit_head() + "\n" + _theorems())


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
