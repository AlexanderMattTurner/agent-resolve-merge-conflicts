"""Finite-state model of the conflict ledger's disposition rule.

The policy it models is `ConflictSet.claim` in
`.github/resolver/auto-resolve/_conflict_set.py`.
`tests/test_ledger_equivalence.py` proves this table and that method agree over
the model's whole reachable set; `tests/_ledger_fsm_tla.py` prints the same
table as `docs/tla/ConflictLedger.tla`, which TLC checks in CI.

PROBLEM CLASS — the state of one merge used to live in about twenty parallel
bash arrays in `prepare.sh`, so a path could sit in two of them at once and
nothing said which pass owned it. The ledger holds ONE disposition per path,
which makes that overlap unrepresentable. This model is where that becomes a
theorem instead of a comment.

Two shapes of drift are removed here rather than watched:

  * The disposition values are `Claimed`'s own members, split into the three
    classes the claim rule reads. A member added there and left unclassified
    raises below rather than escaping every theorem.
  * The TO_MODEL prompts are `PROMPTS`, the set the resolver can answer under.

Each path is independent: one claim reads and writes ONE path's fields. A
REFUSED claim's `reason` is free text the rule never reads, so no field carries
it. `handed` and the `ran_<pass>` flags are the model's only history, recorded
so a witness can name a state the current dispositions alone cannot describe.
"""

from typing import NamedTuple

from tests._conflict_ledger import conflict_set
from tests._fsm_core import T, TrSpec, _compile_machine, _eq, _upd
from tests._fsm_core import explore as _explore
from tests._fsm_core import successors as _successors

Claimed = conflict_set.Claimed

# The declaration order of `Claimed`, so the emitted domain is stable across
# runs and a member added there appears without anyone widening a literal list.
CLAIMED_VALUES: tuple[str, ...] = tuple(str(member) for member in Claimed)
PROMPTS: tuple[str, ...] = tuple(sorted(conflict_set.PROMPTS))

UNCLAIMED: str = str(Claimed.UNCLAIMED)
DEFERRED: str = str(Claimed.DEFERRED)
# The last word on a path. Stated here as the SPECIFICATION — which claims the
# ledger must refuse a second time — and deliberately not read from
# `_conflict_set`'s own set, so `tests/test_ledger_equivalence.py` compares two
# independent statements of one rule.
TERMINAL: tuple[str, ...] = (
    str(Claimed.STAGED),
    str(Claimed.REFUSED),
    str(Claimed.TO_MODEL),
)
# Which state owns which argument field, mirroring `Disposition.__post_init__`.
ARGUMENT_FIELD: dict[str, str] = {DEFERRED: "to", str(Claimed.TO_MODEL): "prompt"}

if set(CLAIMED_VALUES) != {UNCLAIMED, DEFERRED, *TERMINAL}:
    # A `Claimed` member with no class here would get transitions from no rule,
    # and every theorem below would then pass over a disposition the shipped
    # ledger can hold.
    raise SystemExit(
        f"Claimed carries {sorted(CLAIMED_VALUES)}, and this model classifies"
        f" {sorted({UNCLAIMED, DEFERRED, *TERMINAL})}. Give the new member a"
        " rule — terminal, deferred, or the unclaimed start — before the table."
    )

# Two paths is the smallest ledger that can hold one path's whole claim history
# beside another path nobody judged, which is what the stuck witness needs.
PATHS: tuple[str, ...] = ("1", "2")
# The deterministic passes that claim, named as the shipped ledger's callers
# name themselves. Three, not two, so "a pass other than the one the deferral
# names" is a real choice rather than the only other pass.
PASSES: tuple[str, ...] = ("mergiraf", "prepare", "bundle")
# One REFUSED reason, because `claim` never reads the text and `Disposition`
# only requires it to be non-empty.
REASON = "unmergeable"


class Target(NamedTuple):
    """One claim a pass can make: the disposition and the argument it carries."""

    name: str
    claimed: str
    to: str = ""
    prompt: str = ""
    reason: str = ""


TARGETS: tuple[Target, ...] = (
    Target("staged", str(Claimed.STAGED)),
    Target("refused", str(Claimed.REFUSED), reason=REASON),
    *(Target(f"deferred_to_{q}", DEFERRED, to=q) for q in PASSES),
    *(Target(f"to_model_{r}", str(Claimed.TO_MODEL), prompt=r) for r in PROMPTS),
)

_FIELDS: list[tuple[str, type]] = [
    field
    for i in PATHS
    for field in (
        (f"d{i}", str),
        (f"by{i}", str),
        (f"to{i}", str),
        (f"prompt{i}", str),
        (f"handed{i}", bool),
    )
] + [(f"ran_{p}", bool) for p in PASSES]
Ldg = NamedTuple("Ldg", _FIELDS)  # type: ignore[misc]
Ldg.__doc__ = (
    "One ledger: per path, its disposition, the pass that claimed it, the pass a"
    " deferral names, the prompt a TO_MODEL claim names, and whether the claim"
    " landed on top of a deferral. `ran_<pass>` records that a pass claimed"
    " something."
)

FIELD_DOMAINS: dict[str, tuple[object, ...]] = {
    **{f"d{i}": CLAIMED_VALUES for i in PATHS},
    **{f"by{i}": ("", *PASSES) for i in PATHS},
    **{f"to{i}": ("", *PASSES) for i in PATHS},
    **{f"prompt{i}": ("", *PROMPTS) for i in PATHS},
    **{f"handed{i}": (False, True) for i in PATHS},
    **{f"ran_{p}": (False, True) for p in PASSES},
}


def start() -> Ldg:
    """A fresh ledger: every path UNCLAIMED, no pass has claimed anything."""
    return Ldg(
        **{f"d{i}": UNCLAIMED for i in PATHS},
        **{f"by{i}": "" for i in PATHS},
        **{f"to{i}": "" for i in PATHS},
        **{f"prompt{i}": "" for i in PATHS},
        **{f"handed{i}": False for i in PATHS},
        **{f"ran_{p}": False for p in PASSES},
    )


def _transition(path: str, by: str, target: Target, *, after_deferral: bool) -> TrSpec:
    """BY's claim of PATH as TARGET, from an unclaimed path or from a deferral.

    Two transitions rather than one because the guard language is a conjunction:
    "unclaimed, or deferred to this very pass" is two rules, and writing them
    apart is also what lets `handed` mark the second.
    """
    guard = (
        (_eq(f"d{path}", DEFERRED), _eq(f"to{path}", by))
        if after_deferral
        else (_eq(f"d{path}", UNCLAIMED),)
    )
    return TrSpec(
        f"claim_{path}_{by}_{target.name}"
        + ("_after_deferral" if after_deferral else ""),
        guard,
        (
            "update",
            _upd(
                **{
                    f"d{path}": target.claimed,
                    f"by{path}": by,
                    f"to{path}": target.to,
                    f"prompt{path}": target.prompt,
                    f"handed{path}": after_deferral,
                    f"ran_{by}": True,
                }
            ),
        ),
    )


LEDGER_TRANSITIONS: tuple[TrSpec, ...] = tuple(
    _transition(path, by, target, after_deferral=after)
    for path in PATHS
    for by in PASSES
    for target in TARGETS
    for after in (False, True)
)

LEDGER: list[T] = _compile_machine(LEDGER_TRANSITIONS)


def successors(s: Ldg) -> list[tuple[str, Ldg]]:
    return _successors(s, LEDGER)


def reachable(s: Ldg) -> set[Ldg]:
    """Every ledger reachable from S.

    Finite but NOT acyclic: a pass may defer a path back to a pass that deferred
    it, so the deferred states form cycles the terminal claims are the only exit
    from.
    """
    return _explore([s], successors)


def config(s: Ldg, path: str) -> tuple[str, str, str, str]:
    """PATH's whole entry in S — disposition, claiming pass, deferral target,
    prompt — which is everything `claim` reads and everything it writes."""
    return tuple(getattr(s, f"{f}{path}") for f in ("d", "by", "to", "prompt"))


def with_config(path: str, entry: tuple[str, str, str, str]) -> Ldg:
    """A fresh ledger whose PATH holds ENTRY. Every other field is at its start
    value, which no transition on PATH reads."""
    return start()._replace(
        **{f"{f}{path}": v for f, v in zip(("d", "by", "to", "prompt"), entry)}
    )


def claim_names(path: str, by: str, target: Target) -> set[str]:
    """The transitions that record BY claiming PATH as TARGET — the two guard
    variants, whose guards cannot both hold."""
    base = f"claim_{path}_{by}_{target.name}"
    return {base, f"{base}_after_deferral"}
