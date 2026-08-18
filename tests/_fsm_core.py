"""The declarative transition mini-language the FSM models here are written in.

PROBLEM CLASS — one transition relation maintained by hand in two notations
drifts silently, because nothing runs the second notation on every push. A
`TrSpec` table is the single source: `_compile_machine` interprets it as
Python, and `tests/_ladder_fsm_tla.py` prints the same table as a TLA+ module,
kept honest by a round-trip test.

The language carries exactly the forms the tables here use, and the emitter
implements the same set:

  * An atom, over one state: ("eq", field, value), or ("is", field) on a
    boolean.
  * A value spec: ("lit", v), or ("cond", atom, then_spec, else_spec).
  * A step: ("update", updates), where an update is (field, value spec).

Every form is total on both sides, so a step has exactly one successor. A form
added to one notation and not the other raises `ValueError` rather than
emitting something no engine reads — and a NEW form (a nondeterministic choice,
a field read, a named macro) belongs in both at once or in neither.
"""

from collections.abc import Callable
from typing import NamedTuple, TypeVar

_S = TypeVar("_S")

Atom = tuple[object, ...]
ValSpec = tuple[object, ...]
Update = tuple[str, ValSpec]


class T(NamedTuple):
    """A guarded transition; `step` returns the successor."""

    name: str
    guard: Callable[..., bool]
    step: Callable[..., object]


class TrSpec(NamedTuple):
    """One transition, as data both notations are derived from."""

    name: str
    guard: tuple[Atom, ...]  # a conjunction
    step: tuple[object, ...]


def _eq(f: str, v: object) -> Atom:
    return ("eq", f, v)


def _is(f: str) -> Atom:
    return ("is", f)


def _upd(**kw: object) -> tuple[Update, ...]:
    """Literal-only updates, the common case."""
    return tuple((f, ("lit", v)) for f, v in kw.items())


def _compile_atom(a: Atom) -> Callable[..., bool]:
    tag, rest = a[0], a[1:]
    if tag == "eq":
        f, v = rest
        return lambda s: getattr(s, str(f)) == v
    if tag == "is":
        (f,) = rest
        return lambda s: bool(getattr(s, str(f)))
    raise ValueError(a)


def _compile_valspec(vs: ValSpec) -> Callable[..., object]:
    tag, rest = vs[0], vs[1:]
    if tag == "lit":
        (v,) = rest
        return lambda s: v
    if tag == "cond":
        atom, then_vs, else_vs = rest
        cond = _compile_atom(atom)
        then_fn = _compile_valspec(then_vs)
        else_fn = _compile_valspec(else_vs)
        return lambda s: then_fn(s) if cond(s) else else_fn(s)
    raise ValueError(vs)


def _compile_step(step: tuple[object, ...]) -> Callable[..., object]:
    if step[0] != "update":
        raise ValueError(step)
    updates = [(f, _compile_valspec(vs)) for f, vs in step[1]]
    return lambda s: s._replace(**{f: fn(s) for f, fn in updates})


def _compile_machine(specs: tuple[TrSpec, ...]) -> list[T]:
    out = []
    for sp in specs:
        fns = tuple(_compile_atom(a) for a in sp.guard)
        guard = lambda s, fns=fns: all(fn(s) for fn in fns)  # noqa: E731
        out.append(T(sp.name, guard, _compile_step(sp.step)))
    return out


def successors(s, ts: list[T]) -> list[tuple[str, object]]:
    """Every (transition name, successor) pair enabled at S."""
    return [(t.name, t.step(s)) for t in ts if t.guard(s)]


def explore(starts: list[_S], succ: Callable[[_S], list[tuple[str, _S]]]) -> set[_S]:
    """Every state reachable from STARTS."""
    seen = set(starts)
    frontier = list(starts)
    while frontier:
        s = frontier.pop()
        for _, nxt in succ(s):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return seen
