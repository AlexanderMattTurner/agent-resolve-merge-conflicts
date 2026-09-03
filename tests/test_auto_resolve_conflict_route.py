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

# Every disposition `route` can return, read from the module rather than retyped.
# `route` returns these very objects, and `bucket_of` maps each to a prepare.sh
# array, so a retyped copy here would be a second answer that has to agree with
# the shipped one by hand — the class this whole refactor removes. What the pins
# below assert is the MAPPING from an input to one of them, which is the part a
# reader of the module cannot check.
REFUSED_LOCKFILE = conflict_set.REFUSED_LOCKFILE
REFUSED_UNMERGEABLE = conflict_set.REFUSED_UNMERGEABLE
REFUSED_BOTH_DELETED = conflict_set.REFUSED_BOTH_DELETED
DEFERRED_LOCKFILE = conflict_set.DEFERRED_LOCKFILE
DEFERRED_REGEN = conflict_set.DEFERRED_REGEN
DEFERRED_TO_MERGIRAF = conflict_set.DEFERRED_TO_MERGIRAF
TO_MODEL_KEEP_OR_DELETE = conflict_set.TO_MODEL_KEEP_OR_DELETE
TO_MODEL_MARKERS = conflict_set.TO_MODEL_MARKERS
EVERY_OUTCOME = frozenset(
    {
        REFUSED_LOCKFILE,
        REFUSED_UNMERGEABLE,
        REFUSED_BOTH_DELETED,
        DEFERRED_LOCKFILE,
        DEFERRED_REGEN,
        DEFERRED_TO_MERGIRAF,
        TO_MODEL_KEEP_OR_DELETE,
        TO_MODEL_MARKERS,
    }
)

# One pinned answer per shape, for a path carrying no other fact. The three
# shapes where exactly one side holds a version share the keep-or-delete
# verdict, because there is no second version to merge with. A path neither
# side kept reaches this chain only when `git rm` would not stage its deletion,
# so it is a human's. Add/add and both-modified go to the structural pass.
ROUTE_BY_SHAPE = {
    Shape.BOTH_MODIFIED: DEFERRED_TO_MERGIRAF,
    Shape.MODIFY_DELETE: TO_MODEL_KEEP_OR_DELETE,
    Shape.BOTH_DELETED: REFUSED_BOTH_DELETED,
    Shape.ADD_ADD: DEFERRED_TO_MERGIRAF,
    Shape.ADDED_BY_US: TO_MODEL_KEEP_OR_DELETE,
    Shape.ADDED_BY_THEM: TO_MODEL_KEEP_OR_DELETE,
}

# One pinned answer per merge policy, each with the `unmergeable` fact
# `classify` derives from it. A driver-merged path skips the structural pass
# and goes straight to the model, because the worktree already holds what the
# driver wrote.
ROUTE_BY_POLICY = {
    MergePolicy.PLAIN: DEFERRED_TO_MERGIRAF,
    MergePolicy.DRIVER: TO_MODEL_MARKERS,
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
            assert route(facts, **flags) == DEFERRED_LOCKFILE


def test_every_outcome_the_router_can_reach_is_reached_by_some_input():
    assert {route(facts, **flags) for facts, flags in _space()} == EVERY_OUTCOME


def test_every_outcome_names_the_prepare_array_it_partitions_into():
    """Totality in the other direction: an arm added with no bucket row would
    hand prepare.sh a name its `case` refuses, which stops the run rather than
    landing bytes no pass judged."""
    buckets = {conflict_set.bucket_of(outcome) for outcome in EVERY_OUTCOME}
    assert buckets == {
        "skip",
        "deferred_regen",
        "unresolvable",
        "modify_delete",
        "driver_bound",
        "structural",
    }


def test_recognizing_a_lockfile_on_its_own_changes_no_route():
    for facts, flags in _space():
        flipped = replace(facts, lockfile=not facts.lockfile)
        assert route(flipped, **flags) == route(facts, **flags)
