"""PROBLEM CLASS — a resolved conflict region holds a line NEITHER side wrote.

`_out_of_conflict` gates the text OUTSIDE every conflict region, where the two
parents agree and the mechanical merge is the answer. INSIDE a region nothing
deterministic reads what the model wrote. The pre-push merge-delta reviewer is
itself a model, and a run whose credential ladder is spent lands `unverified`
with no read at all.

On agent-glovebox #5430 a shard resolved one line of `evals/ctf/judge.py` to
`count_tool_calls(reading.events)`. Our side wrote `count_tool_calls(events)`,
the base `transcript_tool_calls`, the other side `reading.transcript_tool_calls`.
The committed text was a fourth form that recomputed a value the object already
carried, and a human reading the merge delta was the only thing that caught it.

A line this names is not always wrong: porting one side's rename into the other
side's call legitimately writes a line no side holds. So this REPORTS and never
refuses — `land` names the lines and turns auto-merge off, exactly as an
ambiguous out-of-conflict revert already does — and a resolution whose hunks are
sound still lands.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _conflict_hunks import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    OURS,
    THEIRS,
    Hunk,
    segments,
    side_of,
)
from _git_io import git  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _out_of_conflict import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    MalformedMarkersError,
    MechanicalMergeError,
    PathMissingFromMechanicalTreeError,
    mechanical_tree,
    path_in_tree,
)
from _refusal import fail  # noqa: E402,I001  # pylint: disable=wrong-import-position

# Five ranges is what a reviewer opens the file for. Past that the count carries
# the signal, and the same bound is what `land` already parses on the
# out-of-conflict record.
_RANGES_SHOWN = 5


def written_lines(mechanical_text: str) -> set[str] | None:
    """Every line SOMEBODY wrote in MECHANICAL_TEXT: each conflict region's two
    sides, plus every line outside every region. None when the text carries no
    conflict region, where this has nothing to compare against.

    The `|||||||` base section is NOT a side. Both parents changed that text, so
    a resolution that puts the ancestor's line back writes a line neither parent
    ships, which is the class this reports."""
    parts = segments(mechanical_text)
    if parts is None:
        raise MalformedMarkersError(
            "the mechanical merge's own conflict markers do not parse"
        )
    if not any(isinstance(part, Hunk) for part in parts):
        return None
    written: set[str] = set()
    for part in parts:
        if isinstance(part, Hunk):
            written.update(side_of(part.text, OURS).splitlines())
            written.update(side_of(part.text, THEIRS).splitlines())
        else:
            written.update(part.splitlines())
    return written


def lines_from_neither_side(mechanical_text: str, resolved_text: str) -> list[int]:
    """The 1-based RESOLVED_TEXT line numbers whose text no side of a conflict
    region wrote and the mechanical merge does not hold outside one.

    A blank or whitespace-only line is never reported: it carries no content to
    trace, and `_widened.revert_whitespace_only_edits` owns that class."""
    written = written_lines(mechanical_text)
    if written is None:
        return []
    return [
        number
        for number, line in enumerate(resolved_text.splitlines(), start=1)
        if line.strip() and line not in written
    ]


def describe(numbers: list[int]) -> str:
    """NUMBERS as the range list `land` parses — "12, 15-17", truncated with a
    count so one mangled resolution cannot fill a pull-request comment."""
    groups: list[tuple[int, int]] = []
    for number in numbers:
        if groups and number == groups[-1][1] + 1:
            groups[-1] = (groups[-1][0], number)
        else:
            groups.append((number, number))
    shown = ", ".join(
        str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in groups[:_RANGES_SHOWN]
    )
    rest = len(groups) - _RANGES_SHOWN
    return f"{shown}, and {rest} more" if rest > 0 else shown


def lines_neither_side_wrote(head: str, base: str, paths: list[str]) -> dict[str, str]:
    """PATH -> the range list of its lines that neither side of a conflict region
    wrote, against the mechanical merge of HEAD and BASE.

    A path absent from the WORKTREE was deleted by the resolution and has nothing
    to compare; one absent from the MECHANICAL TREE raises, because that is this
    comparison failing to run rather than finding nothing."""
    tree = mechanical_tree(head, base)
    found: dict[str, str] = {}
    for name in sorted(paths):
        if not Path(name).is_file():
            continue
        if not path_in_tree(tree, name):
            raise PathMissingFromMechanicalTreeError(
                f"'{name}' is absent from the mechanical merge of {head} and {base}"
            )
        numbers = lines_from_neither_side(
            git("show", f"{tree}:{name}"), Path(name).read_text(encoding="utf-8")
        )
        if numbers:
            found[name] = describe(numbers)
    return found


class NeitherSideReport:
    """The APPLICATION of the analysis above to one bundle step.

    A mixin for the reason `OutOfConflictRevert` is one: every method reads the
    step's own resolved set and the two parents it merged."""

    def report_lines_from_neither_side(self) -> None:
        """Name every line inside a conflict region that traces to no side, and
        hand the list to `land` so auto-merge goes off.

        Run over the tree as it will be COMMITTED — after the repo's hooks, whose
        rewrites move every line number below them. Deferred, modify/delete and
        declined paths are excluded for the reasons
        `revert_out_of_conflict_rewrites` excludes them."""
        gated = (
            set(self.allowed)
            - set(self.deferred)
            - set(self.modify_delete)
            - set(self.declined)
        )
        if not gated:
            return
        try:
            found = lines_neither_side_wrote(
                self.checked_out_head, self.merge_base_side, sorted(gated)
            )
        except (
            MechanicalMergeError,
            MalformedMarkersError,
            PathMissingFromMechanicalTreeError,
        ) as exc:
            fail(
                f"the mechanical merge comparison failed: {exc}",
                "the resolution could not be compared against the mechanical "
                "merge, so it was not bundled.",
                resolver_fault=True,
            )
        for name, ranges in sorted(found.items()):
            self.neither_side_lines.append(f"{name}\t{ranges}")
            print(
                f"::warning::the resolution wrote line(s) {ranges} of '{name}' "
                "that neither side of a conflict region carries. Read them as "
                "hand-written code: the merge is landing with auto-merge off so "
                "a human reads them first."
            )
