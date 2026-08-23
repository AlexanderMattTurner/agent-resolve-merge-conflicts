"""The generic FSM-to-TLA+ printer: a `TrSpec` table rendered as TLA+ actions.

PROBLEM CLASS — one transition relation maintained by hand in two notations
drifts silently. `tests/_fsm_core.py` interprets the table as Python; this module
prints the SAME table as TLA+, so a machine's two notations come from one source.

Every emitter here is total on the forms `_fsm_core` compiles, and raises
`ValueError` on a form it does not know rather than printing something no engine
reads. A machine's own module supplies its presentation (`Ctx`), its state
domains, its initial states and its theorems.
"""

import subprocess
from pathlib import Path
from typing import NamedTuple

from tests import _fsm_core as fsm


class Ctx(NamedTuple):
    """One machine's presentation: the variable name and the field spellings."""

    var: str
    fields: dict[str, str]


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
    raise ValueError(a)


def _val(c: Ctx, vs: fsm.ValSpec) -> str:
    tag, rest = vs[0], vs[1:]
    if tag == "lit":
        return _lit(rest[0])
    if tag == "cond":
        atom, then_vs, else_vs = rest
        return f"IF {_atom(c, atom)} THEN {_val(c, then_vs)} ELSE {_val(c, else_vs)}"
    raise ValueError(vs)


def _action(c: Ctx, step: tuple[object, ...]) -> list[str]:
    if step[0] != "update":
        raise ValueError(step)
    items = ", ".join(f"!.{c.fields[f]} = {_val(c, vs)}" for f, vs in step[1])
    return [f"    /\\ {c.var}' = [{c.var} EXCEPT {items}]"]


def _op_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _transition(c: Ctx, sp: fsm.TrSpec) -> str:
    lines = [f"{_op_name(sp.name)} =="]
    lines.extend(f"    /\\ {_atom(c, a)}" for a in sp.guard)
    lines.extend(_action(c, sp.step))
    return "\n".join(lines)


def _disjunction(name: str, ops: list[str]) -> str:
    return f"{name} ==\n" + "\n".join(f"    \\/ {o}" for o in ops)


def _set(values) -> str:
    return "{" + ", ".join(_lit(v) for v in values) + "}"


def _conjunction(name: str, terms: list[str]) -> str:
    return f"{name} ==\n" + "\n".join(f"    /\\ {t}" for t in terms)


def normalized(text: str) -> str:
    """No trailing blanks, exactly one final newline — the whitespace the
    `trailing-whitespace` and `end-of-file-fixer` hooks refuse in a committed
    file. A template that closes on an indented line would otherwise emit a run
    of spaces at EOF, which blocks the commit that regenerates it."""
    return "\n".join(line.rstrip() for line in text.split("\n")).rstrip("\n") + "\n"


def write_module(module_path: Path, text: str) -> None:
    """Write TEXT to MODULE_PATH under the REPO ROOT, and print where it landed.

    The root rather than the caller's cwd: an emitter run from `tests/` would
    otherwise write a second copy there and leave the committed module stale,
    which the freshness test then reports against a file nobody reads."""
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    path = root / module_path
    path.write_text(text, encoding="utf-8")
    print(path)
