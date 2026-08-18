"""Prints `docs/tla/Ladder.tla` from the transition table in
`tests/_ladder_fsm_model.py`, so the Python checker and the TLA+ module cannot
drift. Regenerate after a model edit:

    uv run python -m tests._ladder_fsm_tla

The freshness test in `tests/test_ladder_fsm_tla.py` fails when the committed
module differs from this emitter's output.
"""

import subprocess
from pathlib import Path
from typing import NamedTuple

from tests import _fsm_core as fsm
from tests import _ladder_fsm_model as ladder


class Ctx(NamedTuple):
    """One machine's presentation: the variable name, the field spellings, and
    the macro table a `("macro", name)` atom resolves against."""

    var: str
    fields: dict[str, str]
    macros: dict[str, fsm.Atom]


def _lit(v: object) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    return f'"{v}"'


def _atom(c: Ctx, a: fsm.Atom) -> str:
    tag, rest = a[0], a[1:]
    if tag == "eq":
        return f"{c.var}.{c.fields[str(rest[0])]} = {_lit(rest[1])}"
    if tag == "is":
        return f"{c.var}.{c.fields[str(rest[0])]}"
    if tag in ("and", "or"):
        op = " /\\ " if tag == "and" else " \\/ "
        return "(" + op.join(_atom(c, x) for x in rest) + ")"
    if tag == "macro":
        return str(rest[0])
    raise ValueError(a)


def _val(c: Ctx, vs: fsm.ValSpec) -> str:
    tag, rest = vs[0], vs[1:]
    if tag == "lit":
        return _lit(rest[0])
    if tag == "cur":
        return f"{c.var}.{c.fields[str(rest[0])]}"
    if tag == "cond":
        atom, then_vs, else_vs = rest
        return f"IF {_atom(c, atom)} THEN {_val(c, then_vs)} ELSE {_val(c, else_vs)}"
    raise ValueError(vs)


def _updates(c: Ctx, updates: tuple[fsm.Update, ...]) -> tuple[str | None, str]:
    """Returns (choice binding or None, the EXCEPT item list). At most one
    field may be a nondeterministic choice; it binds to `x`."""
    binding = None
    items = []
    for f, vs in updates:
        if vs[0] == "choice":
            assert binding is None, "one choice field per step"
            opts = ", ".join(_val(c, v) for v in vs[1])
            binding = f"\\E x \\in {{{opts}}} :"
            items.append(f"!.{c.fields[f]} = x")
        else:
            items.append(f"!.{c.fields[f]} = {_val(c, vs)}")
    return binding, ", ".join(items)


def _action(c: Ctx, step: tuple[object, ...]) -> list[str]:
    if step[0] != "update":
        raise ValueError(step)
    binding, items = _updates(c, step[1])
    expr = f"[{c.var} EXCEPT {items}]"
    if binding:
        return [f"    /\\ {binding}", f"           {c.var}' = {expr}"]
    return [f"    /\\ {c.var}' = {expr}"]


def _op_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _transition(c: Ctx, sp: fsm.TrSpec) -> str:
    lines = [f"{_op_name(sp.name)} =="]
    lines.extend(f"    /\\ {_atom(c, a)}" for a in sp.guard)
    lines.extend(_action(c, sp.step))
    return "\n".join(lines)


def _disjunction(name: str, ops: list[str]) -> str:
    return f"{name} ==\n" + "\n".join(f"    \\/ {o}" for o in ops)


LADDER_FIELDS = {f: f for f in ladder.Lg._fields}
MODULE_PATH = Path("docs") / "tla" / "Ladder.tla"


def emit_ladder() -> str:
    c = Ctx("s", LADDER_FIELDS, ladder.LADDER_MACROS)
    domains = []
    for f in ladder.Lg._fields:
        dom = ladder.FIELD_DOMAINS[f]
        text = (
            "BOOLEAN"
            if dom == (False, True)
            else "{" + ", ".join(_lit(v) for v in dom) + "}"
        )
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
(* The credential ladder's retry policy: seven ordered credential rungs.   *)
(* `pos` is the rung about to run, or "DONE" once the walk stops -- the    *)
(* only terminal marker, since every RunI action strictly advances it.     *)
(* Rung 1's zero-cost error always advances (the same-token free retry);   *)
(* every later boundary needs its OWN configured credential.  Python twin: *)
(* tests/_ladder_fsm_model.py, proved against the shipped policy in        *)
(* .github/resolver/auto-resolve/_ladder.py by                             *)
(* tests/test_ladder_equivalence.py.                                       *)
(***************************************************************************)

VARIABLE s

AllStates == [
{domain_rows}
]

\\* Every rung-2..7 CONFIGURED combination, walk not yet begun.
Inits == {{
    [pos |-> "1", o1 |-> "NOT_RUN", o2 |-> "NOT_RUN", o3 |-> "NOT_RUN",
     o4 |-> "NOT_RUN", o5 |-> "NOT_RUN", o6 |-> "NOT_RUN", o7 |-> "NOT_RUN",
     winner |-> "NONE",
     configured2 |-> c2, configured3 |-> c3, configured4 |-> c4,
     configured5 |-> c5, configured6 |-> c6, configured7 |-> c7] :
    c2 \\in BOOLEAN, c3 \\in BOOLEAN, c4 \\in BOOLEAN, c5 \\in BOOLEAN,
    c6 \\in BOOLEAN, c7 \\in BOOLEAN
}}

Init == s \\in Inits

{transitions}

{next_ops}
"""


THEOREMS = """\
\\* Every field stays within its declared domain -- a structural check on the
\\* generated updates, not a restatement of anything Python already proves.
TypeOK == s \\in AllStates

\\* `_ladder.py`'s release rule: at least one rung ran, and every rung that
\\* ran was a PROVEN zero-cost error.  OK and ERR_PAID both count as billed.
AllZeroCost ==
    /\\ s.o1 \\in {"NOT_RUN", "ERR_ZERO"}
    /\\ s.o2 \\in {"NOT_RUN", "ERR_ZERO"}
    /\\ s.o3 \\in {"NOT_RUN", "ERR_ZERO"}
    /\\ s.o4 \\in {"NOT_RUN", "ERR_ZERO"}
    /\\ s.o5 \\in {"NOT_RUN", "ERR_ZERO"}
    /\\ s.o6 \\in {"NOT_RUN", "ERR_ZERO"}
    /\\ s.o7 \\in {"NOT_RUN", "ERR_ZERO"}

AnyRan ==
    \\/ s.o1 # "NOT_RUN" \\/ s.o2 # "NOT_RUN" \\/ s.o3 # "NOT_RUN"
    \\/ s.o4 # "NOT_RUN" \\/ s.o5 # "NOT_RUN" \\/ s.o6 # "NOT_RUN"
    \\/ s.o7 # "NOT_RUN"

Released == AnyRan /\\ AllZeroCost

\\* The walk never continues past a winner.
WinnerImpliesDone == s.winner # "NONE" => s.pos = "DONE"

\\* A released mark never co-occurs with a real (paid) answer.
ReleasedImpliesNoWinner == Released => s.winner = "NONE"

\\* No rung past the free-retry boundary runs without its OWN credential.
RungRanRequiresConfigured ==
    /\\ (s.o3 # "NOT_RUN" => s.configured3)
    /\\ (s.o4 # "NOT_RUN" => s.configured4)
    /\\ (s.o5 # "NOT_RUN" => s.configured5)
    /\\ (s.o6 # "NOT_RUN" => s.configured6)
    /\\ (s.o7 # "NOT_RUN" => s.configured7)

\\* Rung 2 without its own credential is reached only via the free retry.
Rung2NeedsConfiguredOrFreeRetry ==
    (s.o2 # "NOT_RUN" /\\ ~s.configured2) => s.o1 = "ERR_ZERO"

\\* Rule 5: a wall-clock-only failure never advances, at ANY rung -- unlike
\\* ERR_PAID, which steps to the next rung whenever it holds its own
\\* configured credential. A fresh credential faces the identical wall.
NoAdvanceFromWall ==
    /\\ (s.o1 = "ERR_WALL" => s.pos = "DONE")
    /\\ (s.o2 = "ERR_WALL" => s.pos = "DONE")
    /\\ (s.o3 = "ERR_WALL" => s.pos = "DONE")
    /\\ (s.o4 = "ERR_WALL" => s.pos = "DONE")
    /\\ (s.o5 = "ERR_WALL" => s.pos = "DONE")
    /\\ (s.o6 = "ERR_WALL" => s.pos = "DONE")
    /\\ (s.o7 = "ERR_WALL" => s.pos = "DONE")

Inv ==
    /\\ TypeOK
    /\\ WinnerImpliesDone
    /\\ ReleasedImpliesNoWinner
    /\\ RungRanRequiresConfigured
    /\\ Rung2NeedsConfiguredOrFreeRetry
    /\\ NoAdvanceFromWall

\\* At most one attempt per rung: once an outcome is recorded, no later step
\\* changes it -- the walk never revisits a rung it already ran.
AtMostOneAttempt ==
    /\\ [][ s.o1 # "NOT_RUN" => s'.o1 = s.o1 ]_s
    /\\ [][ s.o2 # "NOT_RUN" => s'.o2 = s.o2 ]_s
    /\\ [][ s.o3 # "NOT_RUN" => s'.o3 = s.o3 ]_s
    /\\ [][ s.o4 # "NOT_RUN" => s'.o4 = s.o4 ]_s
    /\\ [][ s.o5 # "NOT_RUN" => s'.o5 = s.o5 ]_s
    /\\ [][ s.o6 # "NOT_RUN" => s'.o6 = s.o6 ]_s
    /\\ [][ s.o7 # "NOT_RUN" => s'.o7 = s.o7 ]_s

\\* Winner uniqueness: once chosen, the winner never changes.
WinnerStable == [][ s.winner # "NONE" => s'.winner = s.winner ]_s

\\* Non-vacuity witnesses (EXPECT-EXIT 12 in the .cfg): TLC's counterexample
\\* trace over the negation IS the reachability proof.
NoFreeRetry == ~(s.o1 = "ERR_ZERO" /\\ ~s.configured2 /\\ s.o2 # "NOT_RUN")
NoFullPaidWalk ==
    ~(/\\ s.o1 = "ERR_PAID" /\\ s.o2 = "ERR_PAID" /\\ s.o3 = "ERR_PAID"
      /\\ s.o4 = "ERR_PAID" /\\ s.o5 = "ERR_PAID" /\\ s.o6 = "ERR_PAID"
      /\\ s.o7 = "ERR_PAID"
      /\\ s.pos = "DONE")

\\* A wall-clock-only failure at rung 3 ends the walk even though rung 4 has
\\* its OWN distinct credential configured -- the stop is ERR_WALL's doing,
\\* not an absent credential's, which is what tells it apart from ERR_PAID.
NoWallDespiteConfigured == ~(s.o3 = "ERR_WALL" /\\ s.configured4 /\\ s.pos = "DONE")

=============================================================================
"""


def normalized(text: str) -> str:
    """No trailing blanks, exactly one final newline — the whitespace the
    `trailing-whitespace` and `end-of-file-fixer` hooks refuse in a committed
    file. A template that closes on an indented line would otherwise emit a run
    of spaces at EOF, which blocks the commit that regenerates it."""
    return "\n".join(line.rstrip() for line in text.split("\n")).rstrip("\n") + "\n"


def module_text() -> str:
    """The committed module's whole content: the generated transition table,
    then the hand-written theorems the configs name."""
    return normalized(emit_ladder() + "\n" + THEOREMS)


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
