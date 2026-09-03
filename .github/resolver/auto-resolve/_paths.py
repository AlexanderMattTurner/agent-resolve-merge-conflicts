"""Every question the resolver asks ABOUT A PATH, answered once.

PROBLEM CLASS — one question with several answers. `is_unmergeable` existed three
times; the merge attribute was read with three different acceptance sets; the
lockfile registry and the caller's ownership table classified the same lockfile
differently depending on which pass asked. A pass added later had to agree with
every pass already there, by hand, and the disagreements were silent.

One classification, computed once per merge, is what a later pass reads instead
of re-deriving. `classify` is the whole of it; the CLI below is how prepare.sh
reads the same answer rather than shelling out per predicate.
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    bind_repo,
    git,
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

#: Security-sensitive trees, so a conflicted file's prompt injection cannot reach
#: the supervision stack. The default names only the trees EVERY consumer has,
#: because this resolver runs against repositories whose layouts it does not
#: know; a caller with more to protect passes `protected-paths-regex`.
PROTECTED_ENV = "AUTO_RESOLVE_PROTECTED_RE"
PROTECTED_DEFAULT = r"^(\.github/|\.claude/|\.hooks/)"

#: Where the resolver's own Claude Code process may not WRITE, whatever a hook
#: grants: the harness refuses Edit/Write there and a hook `allow` does not
#: outrank it. These route through the sidecar prompt instead. An EMPTY override
#: disables the class, which is why it reads with `os.environ.get`'s default
#: rather than falling back on a blank value.
HARNESS_UNWRITABLE_ENV = "AUTO_RESOLVE_HARNESS_UNWRITABLE_RE"
HARNESS_UNWRITABLE_DEFAULT = r"^(\.claude/|\.pre-commit-config\.yaml$)"


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

    def __post_init__(self) -> None:
        # INVARIANT — every stage set git can report maps to exactly one Shape.
        # A path with neither side, or an add/add missing a side, is one git
        # never writes, and admitting it would leave `shape` guessing.
        if self.ours is None and self.theirs is None:
            raise ValueError("a conflicted path holds stage 2, stage 3, or both")
        if self.base is None and (self.ours is None or self.theirs is None):
            raise ValueError("a path with no stage 1 holds both added sides")

    @property
    def shape(self) -> Shape:
        """The shape these stages record. Total, because `__post_init__` refuses
        the stage sets that would have no shape."""
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
    binary: bool
    unmergeable: bool
    protected: bool
    harness_unwritable: bool
    generated_owned: bool
    lockfile: bool


def _matches(path: str, env: str, default: str) -> bool:
    pattern = os.environ.get(env, default)
    return bool(pattern) and re.search(pattern, path) is not None


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


def _binary_to_git(path: str) -> bool:
    """Whether git reports PATH as binary between the two merged sides, so no
    marker-based resolution of it exists. Callable only mid-merge."""
    numstat = git("diff", "--numstat", "HEAD", "MERGE_HEAD", "--", path)
    return numstat.split("\t")[0] == "-" if numstat else False


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
    facts = {}
    for path in paths:
        merge_policy = policy[path]
        # Probed only for a path this merge actually left conflicted: `binary`
        # here means "git wrote no markers for it", which is a statement about
        # the conflict. Asking it of every writable candidate would spend one
        # `git diff` per file the pull request touched and answer nothing.
        binary = (
            path in known
            and merge_policy is not MergePolicy.UNMERGEABLE
            and _binary_to_git(path)
        )
        facts[path] = PathFacts(
            path=path,
            shape=known[path].shape if path in known else Shape.BOTH_MODIFIED,
            policy=merge_policy,
            binary=binary,
            unmergeable=merge_policy is MergePolicy.UNMERGEABLE or binary,
            protected=_matches(path, PROTECTED_ENV, PROTECTED_DEFAULT),
            harness_unwritable=_matches(
                path, HARNESS_UNWRITABLE_ENV, HARNESS_UNWRITABLE_DEFAULT
            ),
            generated_owned=owned.covers(path),
            lockfile=lockfile_rule_for(path) is not None,
        )
    return facts


def flags_of(facts: PathFacts) -> str:
    """FACTS as the comma-separated set the CLI prints, for a shell reader."""
    named = {
        "unmergeable": facts.unmergeable,
        "binary": facts.binary,
        "driver": facts.policy is MergePolicy.DRIVER,
        "modify_delete": facts.shape is Shape.MODIFY_DELETE,
        "add_add": facts.shape is Shape.ADD_ADD,
        "protected": facts.protected,
        "harness_unwritable": facts.harness_unwritable,
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
