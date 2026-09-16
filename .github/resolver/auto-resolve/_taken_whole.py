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
    reporting.
    """
    merge = parents[0]
    done = git_result("merge-base", parents[1], parents[2])
    # Unrelated parents have no ancestor and `merge-base` exits non-zero. There
    # is then no "since" to measure a drop against, so this reports nothing
    # rather than guessing at one.
    if done.returncode != 0:
        return {}
    base = done.stdout.strip()
    out: dict[str, TakenWhole] = {}
    for path in paths:
        at_merge = tree_entry(merge, path)
        if at_merge is None:
            continue
        for kept, dropped in ((parents[1], parents[2]), (parents[2], parents[1])):
            if at_merge != tree_entry(kept, path):
                continue
            if tree_entry(dropped, path) != tree_entry(base, path):
                out[path] = TakenWhole(
                    kept[:SHORT_SHA], dropped[:SHORT_SHA], base[:SHORT_SHA]
                )
            break
    return out
