"""Every question the resolver asks ABOUT A PATH, answered once.

PROBLEM CLASS — one question with several answers. A pass that re-derives a
path's shape, its merge attribute or its ownership has to agree by hand with
every other pass, and a disagreement is silent: each reader looks correct on its
own, and the two verdicts meet only in a resolution nobody checks.

`classify` answers all of them together, once per merge, and every pass reads
that. The CLI below is how the shell steps read the same answer rather than
shelling out per predicate.
"""

import argparse
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    bind_repo,
    git,
    git_status,
)
from _lockfiles import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    rule_for as lockfile_rule_for,
)
from _merge_attr import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    MergePolicy,
    policies,
)
from _owned import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    EMPTY,
    Owned,
    parse as parse_owned,
)


class Shape(StrEnum):
    """What the index holds for a conflicted path, which decides who resolves it.

    One member per unmerged state git can leave, so no state reads as another.
    """

    BOTH_MODIFIED = "both_modified"
    """Stages 1, 2 and 3. Git wrote conflict markers."""
    MODIFY_DELETE = "modify_delete"
    """Stage 1 and exactly one side. NO markers: the file LOOKS resolved, and the
    verdict is keep-or-delete rather than an edit."""
    BOTH_DELETED = "both_deleted"
    """Stage 1 alone: the merge base's version, which NEITHER side kept. Nothing
    is left to edit and nothing is left to keep, so the resolution is the
    deletion git already holds."""
    ADD_ADD = "add_add"
    """No stage 1. Both sides created the path independently."""
    ADDED_BY_US = "added_by_us"
    """Stage 2 alone: our side created the path and their side never held it, so
    there is no second version to reconcile it with."""
    ADDED_BY_THEM = "added_by_them"
    """Stage 3 alone, the mirror of ADDED_BY_US. Their side holds the only
    version, and git writes it into the worktree just as it does ours."""


#: Each state git can leave, keyed by which of (base, ours, theirs) the index
#: holds. `git status --porcelain` names the same states with the two letters in
#: the comments. A key outside this table is a path git left merged, which no
#: reader here asks about.
_SHAPE_BY_STAGES = {
    (True, True, True): Shape.BOTH_MODIFIED,  # UU
    (True, True, False): Shape.MODIFY_DELETE,  # UD, deleted by them
    (True, False, True): Shape.MODIFY_DELETE,  # DU, deleted by us
    (True, False, False): Shape.BOTH_DELETED,  # DD
    (False, True, True): Shape.ADD_ADD,  # AA
    (False, True, False): Shape.ADDED_BY_US,  # AU
    (False, False, True): Shape.ADDED_BY_THEM,  # UA
}


@dataclass(frozen=True, kw_only=True, slots=True)
class Stages:
    """One conflicted path's three index stages, as object ids."""

    base: str | None
    ours: str | None
    theirs: str | None

    @property
    def shape(self) -> Shape:
        return _SHAPE_BY_STAGES[
            (self.base is not None, self.ours is not None, self.theirs is not None)
        ]


@dataclass(frozen=True, kw_only=True, slots=True)
class PathFacts:
    """Everything the resolver knows about one path before any pass touches it."""

    path: str
    shape: Shape
    policy: MergePolicy
    unmergeable: bool
    generated_owned: bool
    #: A path some rule in `_lockfiles` recognizes, whatever its merge attribute.
    #: `_unmergeable.refuse_unmergeable` reads it to keep such a path out of the
    #: model's set: only re-running the lock command produces correct bytes, and
    #: `unmergeable` misses one git left as an ordinary text conflict.
    lockfile: bool


def unmerged_stages() -> dict[str, Stages]:
    """Every path this merge left conflicted, with its three index stages.

    `-z` so a name carrying whitespace, a quote or a newline survives the read:
    the default output C-quotes such a name, and every reader downstream then
    holds a path that names no file.
    """
    found: dict[str, dict[int, str]] = {}
    for record in git("ls-files", "-u", "-z").split("\0"):
        if not record:
            continue
        meta, tab, path = record.partition("\t")
        if not tab:
            raise ValueError(f"unreadable `git ls-files -u -z` record: {record!r}")
        _mode, oid, stage = meta.split(" ")
        sides = found.setdefault(path, {})
        # INVARIANT — one object id per path per stage. A repeated stage would
        # make "exactly one entry per path" a question of which record won.
        if int(stage) in sides:
            raise ValueError(f"{path}: git reported stage {stage} twice")
        sides[int(stage)] = oid
    return {
        path: Stages(base=oids.get(1), ours=oids.get(2), theirs=oids.get(3))
        for path, oids in found.items()
    }


def _binary_to_git(paths: list[str]) -> set[str]:
    """Which of PATHS git reports as binary between the two merged sides, so no
    marker-based resolution of one exists.

    Asked of every path, not only the ones the index still holds unmerged: a pass
    that staged its own resolution leaves the path out of `git ls-files -u`, and
    a probe keyed on that set would then call a binary file mergeable and hand it
    to a model. One `git diff` answers the whole batch, so the wider question
    costs one call rather than one per path.

    Empty outside a merge, where the two sides have no name to compare.

    `-z` because the default output C-quotes a name carrying whitespace or a
    quote, and `--no-renames` because a rename record writes two paths into one
    line and no caller here asks about renames.
    """
    if not paths or git_status("rev-parse", "-q", "--verify", "MERGE_HEAD") != 0:
        return set()
    records = git(
        "diff", "--numstat", "-z", "--no-renames", "HEAD", "MERGE_HEAD", "--", *paths
    ).split("\0")
    named = (record.split("\t", 2) for record in records if record)
    return {path for added, _deleted, path in named if added == "-"}


def classify(
    paths: list[str],
    *,
    base_remote_ref: str,
    owned: Owned = EMPTY,
    stages: dict[str, Stages] | None = None,
) -> dict[str, PathFacts]:
    """PATHS with every per-path question answered, keyed by path.

    The merge attribute is read from BASE_REMOTE_REF rather than the worktree:
    mid-merge the worktree's `.gitattributes` is the pull request's own copy, or
    the marker-riddled file when it too conflicted, and a branch can carry a
    `-merge` line the base has since removed. The base is what a resolution
    merges INTO, so it owns the answer.

    A path with no index stages is not conflicted; it takes BOTH_MODIFIED, which
    is the shape that claims nothing about which side git kept.
    """
    known = unmerged_stages() if stages is None else stages
    policy = policies(paths, source=base_remote_ref)
    binary = _binary_to_git(paths)
    facts = {}
    for path in paths:
        merge_policy = policy[path]
        facts[path] = PathFacts(
            path=path,
            shape=known[path].shape if path in known else Shape.BOTH_MODIFIED,
            policy=merge_policy,
            unmergeable=merge_policy is MergePolicy.UNMERGEABLE or path in binary,
            generated_owned=owned.covers(path),
            lockfile=lockfile_rule_for(path) is not None,
        )
    return facts


#: The shapes whose only resolutions are "keep the file" and "honour the
#: deletion". Exactly one side holds a version, so there is no second version to
#: merge it with and git writes no markers — the file simply LOOKS resolved.
#: Whether the merge base held a version changes what the model reads about the
#: path, never the choice it makes, so all three route to one verdict.
ONE_SIDED_SHAPES = frozenset(
    {Shape.MODIFY_DELETE, Shape.ADDED_BY_US, Shape.ADDED_BY_THEM}
)


def flags_of(facts: PathFacts) -> str:
    """FACTS as the comma-separated set the CLI prints, for a shell reader.

    INVARIANT — every shape git can leave reaches a flag some pass routes on. A
    shape that matches none falls through prepare.sh's partition into the model's
    marker prompt, which for `both_deleted` describes a file the worktree does
    not hold.

    `driver` names a path git merged with a NAMED merge driver, whose output is
    what the worktree holds. It keeps the structural pre-pass off that path: the
    pre-pass re-merges from the three index stages, so it would replace the
    driver's result and report no loss.
    """
    named = {
        "unmergeable": facts.unmergeable,
        "driver": facts.policy is MergePolicy.DRIVER,
        "modify_delete": facts.shape in ONE_SIDED_SHAPES,
        "both_deleted": facts.shape is Shape.BOTH_DELETED,
        "add_add": facts.shape is Shape.ADD_ADD,
        "generated_owned": facts.generated_owned,
    }
    return ",".join(name for name, held in named.items() if held)


def _emit(pairs: list[tuple[str, str]]) -> None:
    """One NUL-terminated `path` then `answer` record per pair.

    NUL and not a tab: the whole reason this classification exists in one place
    is that the shell arrays it replaces could not carry a name with whitespace
    in it, and a tab-separated answer would reintroduce exactly that bound.
    """
    sys.stdout.write("".join(f"{path}\0{answer}\0" for path, answer in pairs))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", default="")
    parser.add_argument("--owned-file", default=None)
    parser.add_argument("--root", default=".")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()

    bind_repo(args.root)
    if not args.base_ref:
        parser.error("--base-ref is required: the merge attribute is read there")
    owned = EMPTY
    if args.owned_file:
        owned = parse_owned(Path(args.owned_file).read_text(encoding="utf-8"))
    facts = classify(args.paths, base_remote_ref=args.base_ref, owned=owned)
    _emit([(path, flags_of(facts[path])) for path in args.paths])


if __name__ == "__main__":
    main()
