"""The declarative transition mini-language every FSM model here is written in.

PROBLEM CLASS — one transition relation maintained by hand in two notations
drifts silently, because nothing runs the second notation on every push. A
`TrSpec` table is the single source: `_compile_machine` interprets it as
Python, and `tests/_ladder_fsm_tla.py` prints the same table as a TLA+ module,
kept honest by a round-trip test.

An atom is a tagged tuple over one state: ("eq", field, value), ("is", field)
on a boolean, ("and"|"or", *atoms), and ("macro", name) for a named predicate in
the machine's macro table. A value spec is ("lit", v), ("cur", field), ("cond",
atom, then_spec, else_spec), or ("choice", specs) for a nondeterministic pick. A
step is ("update", updates).

The language carries exactly the forms the tables here use, and
`tests/_ladder_fsm_tla.py` implements the same set. A form added to one without
the other raises `ValueError` rather than emitting something no engine reads.
"""

from collections.abc import Callable
from itertools import product
from typing import NamedTuple, TypeVar

_S = TypeVar("_S")

Atom = tuple[object, ...]
ValSpec = tuple[object, ...]
Update = tuple[str, ValSpec]


class T(NamedTuple):
    """A guarded transition; `step` returns every nondeterministic successor."""

    name: str
    kind: str  # "ctrl" | "env" | "human"
    guard: Callable[..., bool]
    step: Callable[..., list]


class TrSpec(NamedTuple):
    """One transition, as data both notations are derived from."""

    name: str
    kind: str  # "ctrl" | "env" | "human"
    guard: tuple[Atom, ...]  # a conjunction
    step: tuple[object, ...]


def _eq(f: str, v: object) -> Atom:
    return ("eq", f, v)


def _is(f: str) -> Atom:
    return ("is", f)


def _lit(v: object) -> ValSpec:
    return ("lit", v)


def _upd(**kw: object) -> tuple[Update, ...]:
    """Literal-only updates, the common case."""
    return tuple((f, ("lit", v)) for f, v in kw.items())


def _compile_atom(a: Atom, macros: dict[str, Atom]) -> Callable[..., bool]:
    tag, rest = a[0], a[1:]
    if tag == "eq":
        f, v = rest
        return lambda s: getattr(s, f) == v
    if tag == "is":
        (f,) = rest
        return lambda s: bool(getattr(s, f))
    if tag in ("and", "or"):
        fns = [_compile_atom(x, macros) for x in rest]
        joiner = all if tag == "and" else any
        return lambda s: joiner(fn(s) for fn in fns)
    if tag == "macro":
        return _compile_atom(macros[str(rest[0])], macros)
    raise ValueError(a)


def _compile_valspec(vs: ValSpec, macros: dict[str, Atom]) -> Callable[..., object]:
    tag, rest = vs[0], vs[1:]
    if tag == "lit":
        (v,) = rest
        return lambda s: v
    if tag == "cur":
        (f,) = rest
        return lambda s: getattr(s, f)
    if tag == "cond":
        atom, then_vs, else_vs = rest
        cond = _compile_atom(atom, macros)
        then_fn = _compile_valspec(then_vs, macros)
        else_fn = _compile_valspec(else_vs, macros)
        return lambda s: then_fn(s) if cond(s) else else_fn(s)
    raise ValueError(vs)


def _compile_updates(
    updates: tuple[Update, ...], macros: dict[str, Atom]
) -> Callable[..., list]:
    """Returns an applier producing one successor per choice combination."""
    ValFn = Callable[..., object]
    comp: list[tuple[str, ValFn | list[ValFn]]] = []
    for f, vs in updates:
        if vs[0] == "choice":
            fns = [_compile_valspec(v, macros) for v in vs[1]]
            comp.append((f, fns))
        else:
            comp.append((f, _compile_valspec(vs, macros)))

    def apply(s) -> list:
        per_field = [
            [(f, g(s)) for g in fn] if isinstance(fn, list) else [(f, fn(s))]
            for f, fn in comp
        ]
        return [s._replace(**dict(combo)) for combo in product(*per_field)]

    return apply


def _compile_step(
    step: tuple[object, ...], macros: dict[str, Atom]
) -> Callable[..., list]:
    if step[0] == "update":
        return _compile_updates(step[1], macros)
    raise ValueError(step)


def _compile_machine(specs: tuple[TrSpec, ...], macros: dict[str, Atom]) -> list[T]:
    out = []
    for sp in specs:
        fns = tuple(_compile_atom(a, macros) for a in sp.guard)
        guard = lambda s, fns=fns: all(fn(s) for fn in fns)  # noqa: E731
        out.append(T(sp.name, sp.kind, guard, _compile_step(sp.step, macros)))
    return out


def successors(s, ts: list[T]) -> list[tuple[str, object]]:
    """Every (transition name, successor) pair enabled at S."""
    return [(t.name, out) for t in ts if t.guard(s) for out in t.step(s)]


def explore(
    starts: list[_S], succ: Callable[[_S], list[tuple[str, _S]]]
) -> dict[_S, tuple[_S, str] | None]:
    """Every state reachable from STARTS, each mapped to the (parent, edge)
    that first reached it — `None` for a start, so a trace terminates."""
    seen: dict[_S, tuple[_S, str] | None] = {s: None for s in starts}
    frontier = list(starts)
    while frontier:
        s = frontier.pop()
        for name, nxt in succ(s):
            if nxt not in seen:
                seen[nxt] = (s, name)
                frontier.append(nxt)
    return seen
