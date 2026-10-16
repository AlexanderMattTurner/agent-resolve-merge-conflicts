"""PROBLEM CLASS — the merge carries ONE parent's whole file, and the OTHER
parent changed that same file since the merge base.

Every line of the merged file traces to a parent, so the provenance checks and
the delta review all pass it. No later merge of the base surfaces the drop
either: the dropped side's copy has not moved since, so git sees one side
edited and takes it with no conflict (agent-glovebox#5866 reverted a landed
migration that way).

Both halves of the resolver read this. `remerge-diff-report.py` words it for
the reviewer, and `_contradictory_merge.py` turns it into a finding `land` acts
on.
"""

import sys
from functools import cache
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    git,
    git_result,
)

# How much of a sha a record and a note print. Long enough to name a commit in
# this repository, short enough to read inside a sentence.
SHORT_SHA = 12


class TakenWhole(NamedTuple):
    """One path's one-sided take: the parent whose bytes the merge carries, the
    parent whose change it therefore drops, and the merge base that change is
    measured from. Short shas, ready to print."""

    kept: str
    dropped: str
    base: str


@cache
def tree_entry(rev: str, path: str) -> str | None:
    """The `ls-tree` entry — mode, type and oid — for `path` at `rev`, or None
    when absent. The mode matters: an executable-bit-only flip is a real delta
    that comparing blob oids alone would call superseded.

    Cached: pure in `(rev, path)` within one process, and the whole-file passes
    ask the same question of the same revisions several times per path.
    """
    return git("ls-tree", rev, "--", f":(literal){path}").strip() or None


def taken_whole(parents: list[str], paths: list[str]) -> dict[str, TakenWhole]:
    """The `paths` the MERGE carries one parent's exact bytes for, while the
    OTHER parent changed that file since the parents' merge base — each mapped
    to (the parent kept, the parent dropped, the merge base), so one place words
    what that costs.

    `parents[0]` is the merged tree and may be any tree-ish, so a caller holding
    the resolution in its index passes `git write-tree`'s answer. `parents[1]`
    and `parents[2]` are the two commits merged.

    A take with no such change on the other side says nothing: the two sides
    agreed, and there is no drop to judge. The pairing is what makes this worth
    reporting. With no single merge base this reports nothing rather than
    reading one of the bases arbitrarily.
    """
    base = single_merge_base(parents[1], parents[2])
    if base is None:
        return {}
    out: dict[str, TakenWhole] = {}
    for path in paths:
        at_merge = _object(tree_entry(parents[0], path))
        take = take_of(at_merge, (parents[1], parents[2]), base, path)
        if take is not None:
            out[path] = take
    return out


def single_merge_base(ours: str, theirs: str) -> str | None:
    """The one merge base of OURS and THEIRS, or None when there is no single one.

    Unrelated parents have no ancestor and `merge-base` exits non-zero. A
    criss-cross history has several equally good ancestors, and git merges
    those into a virtual one no single sha names. Neither case gives a "since"
    to measure a drop against."""
    done = git_result("merge-base", "--all", ours, theirs)
    if done.returncode != 0 or len(done.stdout.split()) != 1:
        return None
    return done.stdout.strip()


def _object(entry: str | None) -> str | None:
    """An `ls-tree` entry without its path: the mode, type and oid."""
    return entry.split("\t", 1)[0] if entry else None


def take_of(
    at_merge: str | None, parents: tuple[str, str], base: str, path: str
) -> TakenWhole | None:
    """The one-sided take AT_MERGE makes of PATH, or None when it makes none.

    AT_MERGE is the merged path's mode, type and oid, so an answer still in the
    worktree (`worktree_object`) is judged by the same rule as a merged tree."""
    if at_merge is None:
        return None
    for kept, dropped in (parents, parents[::-1]):
        if at_merge != _object(tree_entry(kept, path)):
            continue
        if tree_entry(dropped, path) == tree_entry(base, path):
            return None
        return TakenWhole(kept[:SHORT_SHA], dropped[:SHORT_SHA], base[:SHORT_SHA])
    return None


def worktree_object(path: str, content: Path) -> str:
    """The mode, type and oid `git add` would record for CONTENT staged at PATH.

    `--path` applies PATH's own attributes, so a clean filter or an eol rule
    yields the oid the commit would carry. The mode is PATH's in the worktree,
    which is where the answer lands."""
    oid = git("hash-object", f"--path={path}", "--", str(content.resolve())).strip()
    mode = "100755" if Path(path).stat().st_mode & 0o100 else "100644"
    return f"{mode} blob {oid}"


def dropped_change(take: TakenWhole, path: str) -> str:
    """What the dropped parent changed in PATH since the base, headed for a
    reader who holds only the kept parent's bytes."""
    return f"What {take.dropped} changed in {path} since {take.base}:\n" + git(
        "diff", take.base, take.dropped, "--", path, check=False
    )
