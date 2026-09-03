"""`route`, the prepare partition's answer, over its WHOLE input space.

prepare.sh sends each conflicted path through a chain of shell tests, and no
reader can check such a chain for totality: a shape or a merge policy added
later reaches no arm and falls out the bottom in silence. These walk every
`Shape`, every `MergePolicy` and every flag combination, and pin one answer per
member of each enum — so a member added with no arm fails the membership
assertion here rather than quietly taking the last arm.
"""

# covers: .github/resolver/auto-resolve/_conflict_set.py

import itertools
from dataclasses import replace

from tests._conflict_ledger import conflict_set, paths as paths_module

Claimed = conflict_set.Claimed
Disposition = conflict_set.Disposition
route = conflict_set.route
MergePolicy = paths_module.MergePolicy
PathFacts = paths_module.PathFacts
Shape = paths_module.Shape

# The three flags prepare holds and `classify` does not, all clear.
CLEAR = {
    "lockfile_refused": False,
    "lockfile_deferred": False,
    "region_deferred": False,
}
# The six independent booleans: the three facts `classify` answers, then CLEAR's.
FLAG_COUNT = 6

# Every disposition `route` can return. The two REFUSED answers differ only in
# their reason, which is also the only thing that shows which arm claimed a
# path that is both an unroutable lockfile and unmergeable.
REFUSED_LOCKFILE = Disposition(
    claimed=Claimed.REFUSED,
    by="prepare",
    reason="no lock command available here regenerates this lockfile",
)
REFUSED_UNMERGEABLE = Disposition(
    claimed=Claimed.REFUSED,
    by="prepare",
    reason="no markers and no textual resolution: only a human settles it",
)
DEFERRED_TO_BUNDLE = Disposition(claimed=Claimed.DEFERRED, by="prepare", to="bundle")
DEFERRED_TO_MERGIRAF = Disposition(
    claimed=Claimed.DEFERRED, by="prepare", to="mergiraf"
)
TO_MODEL_KEEP_OR_DELETE = Disposition(
    claimed=Claimed.TO_MODEL, by="prepare", prompt="modify_delete"
)
EVERY_OUTCOME = frozenset(
    {
        REFUSED_LOCKFILE,
        REFUSED_UNMERGEABLE,
        DEFERRED_TO_BUNDLE,
        DEFERRED_TO_MERGIRAF,
        TO_MODEL_KEEP_OR_DELETE,
    }
)

# One pinned answer per shape, for a path carrying no other fact. Only the
# marker-free modify/delete shape is named by the chain; the four single-stage
# and add/add shapes reach the structural pass beside an ordinary both-modified
# conflict, which is the routing this pins rather than endorses.
ROUTE_BY_SHAPE = {
    Shape.BOTH_MODIFIED: DEFERRED_TO_MERGIRAF,
    Shape.MODIFY_DELETE: TO_MODEL_KEEP_OR_DELETE,
    Shape.BOTH_DELETED: DEFERRED_TO_MERGIRAF,
    Shape.ADD_ADD: DEFERRED_TO_MERGIRAF,
    Shape.ADDED_BY_US: DEFERRED_TO_MERGIRAF,
    Shape.ADDED_BY_THEM: DEFERRED_TO_MERGIRAF,
}

# One pinned answer per merge policy, each with the `unmergeable` fact
# `classify` derives from it. A path bound to a named driver takes the same arm
# as an unbound one: the chain never asks which driver git applied.
ROUTE_BY_POLICY = {
    MergePolicy.PLAIN: DEFERRED_TO_MERGIRAF,
    MergePolicy.DRIVER: DEFERRED_TO_MERGIRAF,
    MergePolicy.UNMERGEABLE: REFUSED_UNMERGEABLE,
}


def _facts(
    shape: Shape,
    policy: MergePolicy = MergePolicy.PLAIN,
    *,
    unmergeable: bool = False,
    generated_owned: bool = False,
    lockfile: bool = False,
) -> PathFacts:
    """One path's facts, every question answered."""
    return PathFacts(
        path="conflicted.txt",
        shape=shape,
        policy=policy,
        unmergeable=unmergeable,
        generated_owned=generated_owned,
        lockfile=lockfile,
    )


def _space() -> list[tuple[PathFacts, dict[str, bool]]]:
    """Every input `route` accepts: both enums crossed with the six booleans."""
    space = []
    for shape, policy, flags in itertools.product(
        Shape, MergePolicy, itertools.product((False, True), repeat=FLAG_COUNT)
    ):
        unmergeable, generated_owned, lockfile, refused, deferred, region = flags
        space.append(
            (
                _facts(
                    shape,
                    policy,
                    unmergeable=unmergeable,
                    generated_owned=generated_owned,
                    lockfile=lockfile,
                ),
                {
                    "lockfile_refused": refused,
                    "lockfile_deferred": deferred,
                    "region_deferred": region,
                },
            )
        )
    return space


def test_every_input_in_the_space_gets_a_decided_disposition():
    space = _space()
    assert len(space) == len(Shape) * len(MergePolicy) * 2**FLAG_COUNT
    for facts, flags in space:
        answer = route(facts, **flags)
        assert isinstance(answer, Disposition)
        assert answer.claimed is not Claimed.UNCLAIMED
        assert answer.by == "prepare"


def test_each_shape_keeps_its_pinned_route():
    assert set(ROUTE_BY_SHAPE) == set(Shape)
    for shape, expected in ROUTE_BY_SHAPE.items():
        assert route(_facts(shape), **CLEAR) == expected


def test_each_merge_policy_keeps_its_pinned_route():
    assert set(ROUTE_BY_POLICY) == set(MergePolicy)
    for policy, expected in ROUTE_BY_POLICY.items():
        facts = _facts(
            Shape.BOTH_MODIFIED,
            policy,
            unmergeable=policy is MergePolicy.UNMERGEABLE,
        )
        assert route(facts, **CLEAR) == expected


def test_the_lockfile_verdict_outranks_every_other_fact():
    for facts, flags in _space():
        if flags["lockfile_refused"]:
            assert route(facts, **flags) == REFUSED_LOCKFILE
        elif flags["lockfile_deferred"]:
            assert route(facts, **flags) == DEFERRED_TO_BUNDLE


def test_every_outcome_the_router_can_reach_is_reached_by_some_input():
    assert {route(facts, **flags) for facts, flags in _space()} == EVERY_OUTCOME


def test_recognizing_a_lockfile_on_its_own_changes_no_route():
    for facts, flags in _space():
        flipped = replace(facts, lockfile=not facts.lockfile)
        assert route(flipped, **flags) == route(facts, **flags)
