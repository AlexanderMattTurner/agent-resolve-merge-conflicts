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
    """What the index holds for a conflicted path, which decides who resolves it."""

    BOTH_MODIFIED = "both_modified"
    """Stages 1, 2 and 3. Git wrote conflict markers."""
    MODIFY_DELETE = "modify_delete"
    """Stage 1 and exactly one side. NO markers: the file LOOKS resolved, and the
    verdict is keep-or-delete rather than an edit."""
    ADD_ADD = "add_add"
    """No stage 1. Both sides created the path independently."""


@dataclass(frozen=True, kw_only=True, slots=True)
class Stages:
    """One conflicted path's three index stages, as object ids."""

    base: str | None
    ours: str | None
    theirs: str | None

    @property
    def shape(self) -> Shape:
        if self.base is None:
            return Shape.ADD_ADD
        if self.ours is None or self.theirs is None:
            return Shape.MODIFY_DELETE
        return Shape.BOTH_MODIFIED


@dataclass(frozen=True, kw_only=True, slots=True)
class PathFacts:
    """Everything the resolver knows about one path before any pass touches it."""

    path: str
    shape: Shape
    policy: MergePolicy
    unmergeable: bool
    generated_owned: bool
    lockfile: bool


def unmerged_stages() -> dict[str, Stages]:
    """Every path this merge left conflicted, with its three index stages.

    `-z` so a name carrying whitespace, a quote or a newline survives the read:
    the default output C-quotes such a name, and every reader downstream then
    holds a path that names no file.
    """
    records = git("ls-files", "-u", "-z").split("\0")[:-1]
    found: dict[str, dict[int, str]] = {}
    for record in records:
        meta, path = record.split("\t", 1)
        _mode, oid, stage = meta.split()
        found.setdefault(path, {})[int(stage)] = oid
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


def flags_of(facts: PathFacts) -> str:
    """FACTS as the comma-separated set the CLI prints, for a shell reader."""
    named = {
        "unmergeable": facts.unmergeable,
        "modify_delete": facts.shape is Shape.MODIFY_DELETE,
        "add_add": facts.shape is Shape.ADD_ADD,
        "generated_owned": facts.generated_owned,
        "lockfile": facts.lockfile,
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
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--classify",
        action="store_true",
        help="print every fact about each path, as a comma-separated flag set",
    )
    mode.add_argument(
        "--shape",
        action="store_true",
        help="print each path's index shape alone, needing no attributes",
    )
    parser.add_argument("--base-ref", default="")
    parser.add_argument("--owned-file", default=None)
    parser.add_argument("--root", default=".")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()

    bind_repo(args.root)
    if args.shape:
        stages = unmerged_stages()
        _emit(
            [
                (path, stages[path].shape if path in stages else "")
                for path in args.paths
            ]
        )
        return
    if not args.base_ref:
        parser.error("--classify needs --base-ref: the merge attribute is read there")
    owned = EMPTY
    if args.owned_file:
        owned = parse_owned(Path(args.owned_file).read_text(encoding="utf-8"))
    facts = classify(args.paths, base_remote_ref=args.base_ref, owned=owned)
    _emit([(path, flags_of(facts[path])) for path in args.paths])


if __name__ == "__main__":
    main()
