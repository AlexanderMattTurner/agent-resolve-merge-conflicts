"""Prints `docs/tla/ConflictLedger.tla` from the transition table in
`tests/_ledger_fsm_model.py`, so the Python checker and the TLA+ module cannot
drift. Regenerate after a model edit:

    uv run python -m tests._ledger_fsm_tla

The freshness test in `tests/test_ledger_fsm_tla.py` fails when the committed
module differs from this emitter's output.
"""

from pathlib import Path

from tests import _ledger_fsm_model as ledger
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

LEDGER_FIELDS = {f: f for f in ledger.Ldg._fields}
MODULE_PATH = Path("docs") / "tla" / "ConflictLedger.tla"


def _init_record() -> str:
    """The one initial state: every path unclaimed, no pass has claimed."""
    per_path = [
        f'd{i} |-> "{ledger.UNCLAIMED}", by{i} |-> "", to{i} |-> "",'
        f' prompt{i} |-> "", handed{i} |-> FALSE'
        for i in ledger.PATHS
    ]
    flags = ", ".join(f"ran_{p} |-> FALSE" for p in ledger.PASSES)
    body = ",\n    ".join([*per_path, flags])
    return f"Init == s = [\n    {body}]"


def _index_fn(name: str, field: str) -> str:
    """One per-path field, read by path index, so the partitions below are sets
    of paths rather than lists of field names."""
    arms = " [] ".join(f"i = {i} -> s.{field}{i}" for i in ledger.PATHS)
    return f"{name} == [i \\in 1..N |-> CASE {arms}]"


def _partition_pairs() -> list[str]:
    values = ledger.CLAIMED_VALUES
    return [
        f"{_op_name(a)} \\cap {_op_name(b)} = {{}}"
        for n, a in enumerate(values)
        for b in values[n + 1 :]
    ]


def _theorems() -> str:
    """The safety scheme, the settled-ledger property, and the two witnesses,
    over whatever dispositions and prompts `_conflict_set` declares."""
    paths = ledger.PATHS
    unclaimed = ledger.UNCLAIMED
    partitions = "\n".join(
        f'{_op_name(v)} == Partition("{v}")' for v in ledger.CLAIMED_VALUES
    )
    covers = " \\cup ".join(_op_name(v) for v in ledger.CLAIMED_VALUES)
    disjoint = _conjunction(
        "DisjointPartitions", [*_partition_pairs(), f"{covers} = 1..N"]
    )
    owned = _conjunction(
        "FieldsOwnedByState",
        [
            f'{_op_name(state)} = {{ i \\in 1..N : {field.capitalize()}[i] # "" }}'
            for state, field in ledger.ARGUMENT_FIELD.items()
        ]
        + [f'{_op_name(unclaimed)} = {{ i \\in 1..N : By[i] = "" }}'],
    )
    never_back = _conjunction(
        "NeverUnclaimedAgain",
        [f'[][ s.d{i} # "{unclaimed}" => s\'.d{i} # "{unclaimed}" ]_s' for i in paths],
    )
    final = _conjunction(
        "TerminalClaimIsFinal",
        [
            f"[][ s.d{i} \\in Terminal =>"
            f" (s'.d{i} = s.d{i} /\\ s'.by{i} = s.by{i} /\\ s'.prompt{i} = s.prompt{i})"
            " ]_s"
            for i in paths
        ],
    )
    all_ran = _conjunction("AllPassesRan", [f"s.ran_{p}" for p in ledger.PASSES])

    return f"""\\* Every field stays within its declared domain -- a structural check on the
\\* generated updates, not a restatement of anything Python already proves.
TypeOK == s \\in AllStates

\\* One path's entry, read by path index.
{_index_fn("Disp", "d")}
{_index_fn("By", "by")}
{_index_fn("To", "to")}
{_index_fn("Prompt", "prompt")}
{_index_fn("Handed", "handed")}

\\* The last word on a path: `claim` refuses a second claim on one of these.
Terminal == {_set(ledger.TERMINAL)}

Partition(c) == {{ i \\in 1..N : Disp[i] = c }}

{partitions}

\\* The property the twenty bash arrays did NOT have: a path could sit in
\\* `llm_list` and `deferred_regen` at once, and nothing said which pass owned
\\* it.  One disposition field per path makes that overlap unrepresentable, so
\\* this holds by construction -- and the construction IS the fix.  It reds if a
\\* later edit gives a path a second way to be in a partition.
{disjoint}

\\* Each argument field belongs to exactly one state, and its state requires it.
\\* That is `Disposition.__post_init__`'s rule, checked here over every state the
\\* generated updates can reach rather than at one constructor call.
{owned}

Inv ==
    /\\ TypeOK
    /\\ DisjointPartitions
    /\\ FieldsOwnedByState

\\* No lost path: a claimed path never goes back to unclaimed.  The other half of
\\* that claim -- a path never leaves the ledger -- is DisjointPartitions' last
\\* line, which holds in every state.
{never_back}

\\* Single claim: a terminal entry never changes again, so a second pass can
\\* never disagree with the pass that had the last word.
{final}

ClaimsAreFinal == NeverUnclaimedAgain /\\ TerminalClaimIsFinal

AllClaimsTerminal == \\A i \\in 1..N : Disp[i] \\in Terminal

\\* Nothing is left to claim: from a ledger whose every path is terminal, no step
\\* changes anything.  Stated over TERMINAL and not over "claimed", because a
\\* DEFERRED path IS claimed and still has one claim to come -- the witness in
\\* ConflictLedger_handoff is that counterexample.
NoClaimOnceSettled == [][ AllClaimsTerminal => UNCHANGED s ]_s

\\* Non-vacuity witnesses (EXPECT-EXIT 12 in the .cfg): TLC's counterexample
\\* trace over the negation IS the reachability proof.

\\* A deferral really is finished by the pass it names.  `handed` is written only
\\* by a claim whose guard read `to = <this pass>`, so a handed STAGED path is one
\\* the deferral's named pass came back for.
NoHandoff == ~(\\E i \\in 1..N : Handed[i] /\\ Disp[i] = "staged")

{all_ran}

\\* A path nobody judged, after every pass has claimed something.  This is the
\\* state `require_fully_dispositioned` refuses, so the trace is what proves that
\\* refusal is not vacuous.
NoStuckPath == ~(AllPassesRan /\\ {_op_name(unclaimed)} # {{}})

=============================================================================
"""


def emit_ledger() -> str:
    c = Ctx("s", LEDGER_FIELDS)
    domains = []
    for f in ledger.Ldg._fields:
        dom = ledger.FIELD_DOMAINS[f]
        text = "BOOLEAN" if dom == (False, True) else _set(dom)
        domains.append(f"    {c.fields[f]}: {text}")
    domain_rows = ",\n".join(domains)
    transitions = "\n\n".join(_transition(c, sp) for sp in ledger.LEDGER_TRANSITIONS)
    next_ops = _disjunction(
        "Next", [_op_name(sp.name) for sp in ledger.LEDGER_TRANSITIONS]
    )
    return f"""--------------------------- MODULE ConflictLedger ---------------------------
(* GENERATED FILE — do not edit. tests/_ledger_fsm_tla.py prints this       *)
(* module from the transition table in tests/_ledger_fsm_model.py;          *)
(* regenerate with `uv run python -m tests._ledger_fsm_tla`.                *)
(***************************************************************************)
(* The conflict ledger's disposition rule: one entry per conflicted path,   *)
(* each holding exactly ONE disposition.  A pass claims a path, which is    *)
(* the only action.  STAGED, REFUSED and TO_MODEL are the last word; a      *)
(* DEFERRED path is claimed again by the one pass its `to` names, and by    *)
(* nobody else.  N is the number of paths this table was generated for.     *)
(* Python twin: tests/_ledger_fsm_model.py, proved against the shipped      *)
(* ledger by tests/test_ledger_equivalence.py.                              *)
(***************************************************************************)

\\* Naturals for `..`: the theorems below range over the path indices 1..N.
EXTENDS Naturals

CONSTANT N

\\* The table below carries one field set per path, so a config that set N to
\\* anything else would read a partition over paths the record does not hold.
ASSUME N = {len(ledger.PATHS)}

VARIABLE s

AllStates == [
{domain_rows}
]

\\* Nothing claimed yet.
{_init_record()}

{transitions}

{next_ops}
"""


def module_text() -> str:
    """The committed module: the generated transition table, then the theorems
    the configs name."""
    return normalized(emit_ledger() + "\n" + _theorems())


def main() -> None:
    write_module(MODULE_PATH, module_text())


if __name__ == "__main__":
    main()
