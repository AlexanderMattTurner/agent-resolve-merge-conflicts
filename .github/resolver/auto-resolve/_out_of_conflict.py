"""PROBLEM CLASS — a merge resolution rewrote lines both parents left identical.

A shard resolving one conflict hunk can also touch text nothing put in
conflict, because nothing stops it from rewriting a line outside the hunk it
was asked to resolve. On agent-glovebox PR #4492 a resolution re-indented a
12-line comment (2 spaces to 8) on lines byte-identical between both parents,
and nothing that only checks the hunks themselves catches an edit outside them.

This diffs the mechanical merge blob (markers included) against the resolved
text and flags every changed line range that falls entirely outside every
conflict span `_conflict_hunks.segments` found. A pure insertion right at a
span boundary is not flagged: `difflib` can anchor it on either side of the
span, so a resolution that legitimately deletes a span and appends replacement
text must not be misread as an out-of-span edit.

`repair_out_of_conflict` then UNDOES those ranges, because outside a span both
parents wrote the same bytes and the mechanical merge is the content. What the
revert cannot answer, the caller lands and reports to a human.
"""

import difflib
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _conflict_hunks import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    MECHANICAL_CONFLICT_STYLE,
    Hunk,
    conflict_style_args,
    segments,
)
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    git,
    git_lines,
    git_status,
)
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    fail,
)

_TREE_OID_RE = re.compile(r"[0-9a-f]{40,64}")


class MechanicalMergeError(Exception):
    """Raised when the two parents' mechanical merge could not be written, so
    there is nothing to compare a resolution against."""


class MalformedMarkersError(Exception):
    """Raised when the mechanical merge's own conflict markers do not parse.

    The most suspicious input this module sees — never read the same as
    "nothing to gate", or the gate fails open on exactly the tree it should be
    strictest about."""


class PathMissingFromMechanicalTreeError(Exception):
    """Raised when a path this run resolved is ABSENT from the mechanical merge
    of its own two parents — a rename or a tree write the resolution made that
    the comparison cannot follow. Distinct from a path merely absent from the
    WORKTREE (a resolution that deleted it), which has nothing to compare and is
    not an error."""


class RepairUnsoundError(Exception):
    """Raised when the revert's own output still differs from the mechanical merge
    outside a conflict span — a defect in this module, never a merge to bundle."""


@dataclass(frozen=True)
class Violation:
    """One changed range difflib found on the mechanical side wholly outside
    every conflict span. `mech_start > mech_end` marks a pure insertion: the
    mechanical side contributed no lines, so its range is empty by convention
    (`mech_start == mech_end + 1`)."""

    mech_start: int
    mech_end: int

    def describe(self) -> str:
        """Where a human looks, in MECHANICAL-merge line numbers.

        The RESOLVED side cannot serve. A deletion contributes no resolved
        lines, so its range there is empty, and printing it produced the
        reversed "32-31" a human read on agent-glovebox PR #4992 — a range
        naming no line of either file, on the one refusal that needs acting on.
        Every arm below names a line that exists: an insertion has no mechanical
        line of its own, so it names the gap it landed in instead."""
        if self.mech_end == 0:
            return "before 1"
        if self.mech_start > self.mech_end:
            return f"between {self.mech_end} and {self.mech_start}"
        if self.mech_start == self.mech_end:
            return str(self.mech_start)
        return f"{self.mech_start}-{self.mech_end}"


@dataclass(frozen=True)
class Offender:
    """One path's out-of-span changes, and the text that undoes them.

    `repaired` is None when the revert is ambiguous, which is the only case the
    caller still has to refuse."""

    violations: list[Violation]
    repaired: str | None


def conflict_spans(mechanical_text: str) -> list[tuple[int, int]] | None:
    """1-based inclusive line ranges of each conflict hunk in MECHANICAL_TEXT,
    marker lines included. None when the text has no hunks at all — a markerless
    mechanical merge (binary, or a whole-file `-merge` keep), where the whole
    file is the conflict region and there is nothing to gate.

    Malformed markers RAISE rather than returning None: they are the most
    suspicious input this module sees, and reading them the same as "nothing to
    gate" would fail the gate open on exactly the tree it should be strictest
    about."""
    parts = segments(mechanical_text)
    if parts is None:
        raise MalformedMarkersError(
            "the mechanical merge's own conflict markers do not parse"
        )
    spans = []
    line = 1
    for part in parts:
        text = part.text if isinstance(part, Hunk) else part
        length = len(text.splitlines())
        if isinstance(part, Hunk):
            spans.append((line, line + length - 1))
        line += length
    return spans if spans else None


def _in_span_with_slop(spans: list[tuple[int, int]], i1: int) -> bool:
    # difflib's 0-based `i1` for an insert already reads as a 1-based "insert
    # after line i1" position (i1 lines of the mechanical side precede it), so
    # no further conversion is needed here.
    return any(start - 1 <= i1 <= end for start, end in spans)


def _uncovered(spans: list[tuple[int, int]], lo: int, hi: int) -> list[tuple[int, int]]:
    """The 1-based inclusive sub-ranges of [LO, HI] that no span covers.

    Clipping rather than an overlap test, because `SequenceMatcher` coalesces the
    deletion of a marker block with a rewrite of the line right after it into ONE
    `replace` opcode. An opcode admitted whole because part of it lands in a span
    is exactly the damage this module exists to catch."""
    out: list[tuple[int, int]] = []
    cursor = lo
    for start, end in sorted(spans):
        if end < cursor:
            continue
        if start > hi:
            break
        if start > cursor:
            out.append((cursor, min(start - 1, hi)))
        cursor = max(cursor, end + 1)
        if cursor > hi:
            return out
    if cursor <= hi:
        out.append((cursor, hi))
    return out


def out_of_conflict_hunks(mechanical_text: str, resolved_text: str) -> list[Violation]:
    """Every changed range RESOLVED_TEXT introduces outside a conflict span in
    MECHANICAL_TEXT. [] when `conflict_spans` finds nothing to gate.

    A `replace`/`delete` opcode is checked by CONTAINMENT, not overlap:
    `SequenceMatcher` coalesces the deletion of a marker block with a rewrite of
    the line right after it into one opcode, and admitting the whole opcode
    because part of it touches a span is exactly the damage this exists to
    catch. `_uncovered` finds the sub-range of the opcode's mechanical side no
    span covers; any such sub-range is a violation. Only a pure `insert` gets
    the boundary allowance, since a resolution that deletes a span and appends
    replacement text must not have its trailing line misattributed."""
    spans = conflict_spans(mechanical_text)
    if spans is None:
        return []
    mech_lines = mechanical_text.splitlines(keepends=True)
    res_lines = resolved_text.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, mech_lines, res_lines, autojunk=False)
    violations = []
    for tag, i1, i2, *_resolved_side in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            if _in_span_with_slop(spans, i1):
                continue
            violations.append(Violation(i1 + 1, i1))
            continue
        for mech_start, mech_end in _uncovered(spans, i1 + 1, i2):
            violations.append(Violation(mech_start, mech_end))
    return violations


def repair_out_of_conflict(mechanical_text: str, resolved_text: str) -> str | None:
    """RESOLVED_TEXT with every out-of-span change put back to MECHANICAL_TEXT's
    own lines. None when the revert would have to guess.

    Outside a conflict span both parents wrote the same bytes, so the mechanical
    merge IS the right content there and restoring it takes no judgement. That is
    what lets a resolution whose hunks are correct land despite a tidy-up the
    shard had no licence to make.

    The guess this refuses to make is an opcode whose mechanical range a span
    covers only in PART: `SequenceMatcher` coalesces a marker block's deletion
    with a rewrite of the line after it, and splitting one opcode would have to
    decide where the resolved replacement ends."""
    spans = conflict_spans(mechanical_text)
    if spans is None:
        return None
    mech_lines = mechanical_text.splitlines(keepends=True)
    res_lines = resolved_text.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, mech_lines, res_lines, autojunk=False)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(res_lines[j1:j2])
            continue
        if tag == "insert":
            # An insert outside every span contributed the lines and nothing
            # else, so dropping them IS the revert.
            if _in_span_with_slop(spans, i1):
                out.extend(res_lines[j1:j2])
            continue
        uncovered = _uncovered(spans, i1 + 1, i2)
        if not uncovered:
            out.extend(res_lines[j1:j2])
        elif uncovered == [(i1 + 1, i2)]:
            out.extend(mech_lines[i1:i2])
        else:
            return None
    repaired = "".join(out)
    if _drops_a_context_line(spans, mech_lines, res_lines, repaired):
        return None
    # The revert answers this module's own question before the caller writes it.
    # RAISED rather than returned: a None here would reach the human as the
    # ambiguity message above, which is a confident and wrong diagnosis of a
    # reconstruction defect.
    if out_of_conflict_hunks(mechanical_text, repaired):
        raise RepairUnsoundError(
            "the reverted text still differs from the mechanical merge outside "
            "every conflict span"
        )
    return repaired


def _drops_a_context_line(
    spans: list[tuple[int, int]],
    mech_lines: list[str],
    res_lines: list[str],
    repaired: str,
) -> bool:
    """Whether the revert removed a line the mechanical merge also holds as context.

    INVARIANT — this is what stops the revert deleting a line the resolution was
    entitled to write. When a resolution replaces a span with text that repeats the
    context right after it, `SequenceMatcher` can match that context to the
    replacement and report the real context as an insertion outside the span. The
    revert then drops one of the two copies, and the gate re-run agrees, because the
    same ambiguous alignment reads the shortened file as correct.

    A line the mechanical merge has OUTSIDE every span is exactly the line whose two
    readings cannot be told apart, so losing one is ambiguity, never a tidy-up."""
    covered = {line for start, end in spans for line in range(start, end + 1)}
    context = Counter(
        text for number, text in enumerate(mech_lines, start=1) if number not in covered
    )
    kept = Counter(repaired.splitlines(keepends=True))
    return any(
        kept[text] < count and text in context
        for text, count in Counter(res_lines).items()
    )


def rewrites_outside_conflicts(
    head: str, base: str, paths: list[str]
) -> dict[str, Offender]:
    """PATH -> its out-of-span changes and the text that undoes them, against the
    mechanical merge of HEAD and BASE. Absent from the result means nothing to
    report for that path.

    `merge.conflictStyle` is pinned so a repository-level diff3 setting cannot
    change the span shapes this compares against. `merge-tree` exit 1 is git's
    conflicted-but-written verdict, which is the normal case here; a tree that
    is not an object id raises rather than reading as "no violations". A path
    absent from the WORKTREE (the resolution deleted it) has nothing to compare
    and is skipped; a path absent from the MECHANICAL TREE raises, because that
    is this comparison failing to run, not finding nothing to report."""
    tree = git(
        *conflict_style_args(MECHANICAL_CONFLICT_STYLE),
        "merge-tree",
        "--write-tree",
        head,
        base,
        check=False,
    ).split("\n", 1)[0]
    if not _TREE_OID_RE.fullmatch(tree):
        raise MechanicalMergeError(f"git merge-tree {head} {base} wrote no tree")
    out: dict[str, Offender] = {}
    for name in sorted(paths):
        if not Path(name).is_file():
            continue
        if git_status("cat-file", "-e", f"{tree}:{name}") != 0:
            raise PathMissingFromMechanicalTreeError(
                f"'{name}' is absent from the mechanical merge of {head} and {base}"
            )
        mechanical = git("show", f"{tree}:{name}")
        # Strict, because this text is now WRITTEN BACK. `git()` decodes the
        # mechanical side strictly too, and a replacement character in a merge
        # commit is a byte neither parent nor the resolver wrote.
        resolved = Path(name).read_text(encoding="utf-8")
        violations = out_of_conflict_hunks(mechanical, resolved)
        if violations:
            out[name] = Offender(
                violations, repair_out_of_conflict(mechanical, resolved)
            )
    return out


class OutOfConflictRevert:
    """The APPLICATION of the analysis above to one bundle step.

    A mixin rather than free functions: every method needs the step's own
    resolved set and the two parents it merged, and threading those through each
    call would state the coupling twice."""

    def revert_out_of_conflict_rewrites(self) -> None:
        """A bundled file should only differ from the mechanical merge INSIDE a
        conflict region, because outside a span both parents wrote the same bytes.

        An out-of-span change is REVERTED wherever the revert needs no judgement,
        which is most of them. Where the revert would have to guess, the run REPORTS
        the change and lands it rather than costing the PR a handoff over hunks that
        were sound: `land` names the lines and turns auto-merge off, so the
        merge-delta reviewer reads them before anyone merges.

        `refuse_edits_outside_the_set` is the same question one level up, over whole
        paths, and cannot see this one: a conflicted file is in the set, so a
        rewrite of its untouched context reads as part of the resolution.

        Deferred paths are excluded because a generator, not the resolver, writes
        them; modify/delete has no text to compare; a declined path keeps the head's
        whole file, which the decline notes report instead."""
        gated = (
            set(self.allowed)
            - set(self.deferred)
            - set(self.modify_delete)
            - set(self.declined)
        )
        if not gated:
            return
        try:
            offenders = rewrites_outside_conflicts(
                self.checked_out_head, self.merge_base_side, sorted(gated)
            )
        except (
            MechanicalMergeError,
            MalformedMarkersError,
            PathMissingFromMechanicalTreeError,
            RepairUnsoundError,
        ) as exc:
            fail(
                f"the mechanical merge comparison failed: {exc}",
                "the resolution could not be compared against the mechanical "
                "merge, so it was not bundled.",
                resolver_fault=True,
            )
        for name, offender in sorted(offenders.items()):
            violations = offender.violations
            shown = ", ".join(v.describe() for v in violations[:5])
            rest = len(violations) - 5
            ranges = f"{shown}, and {rest} more" if rest > 0 else shown
            if offender.repaired is not None:
                # The bundled file now matches the mechanical merge outside every
                # span, so this path reports nothing and auto-merge stays armed.
                Path(name).write_text(offender.repaired, encoding="utf-8")
                git("add", "--", name)
                print(
                    f"::warning::reverted the resolution's out-of-conflict "
                    f"change to '{name}' (mechanical line(s) {ranges}): outside a "
                    "span both parents wrote the same bytes, so the mechanical "
                    "merge is the content, and the hunks this run resolved stand."
                )
                continue
            # The revert would have to guess: a changed block covers a span only in
            # part, or undoing it would drop a line the mechanical merge also holds
            # outside every span. The resolution lands as written and `land` reports
            # it, rather than costing the PR a handoff over hunks that were sound.
            self.out_of_conflict_rewrites.append(f"{name}\t{ranges}")
            print(
                "::warning::the resolution rewrote lines outside every conflict "
                f"region in '{name}' (mechanical line(s) {ranges}) and the revert "
                "was ambiguous, so those lines land as written. Read them as "
                "hand-written code: `git "
                f"{' '.join(conflict_style_args(MECHANICAL_CONFLICT_STYLE))} "
                f"merge-tree --write-tree {self.checked_out_head} "
                f"{self.merge_base_side}` "
                "writes the mechanical merge those line numbers index, and "
                f"`git show <tree>:{name}` prints it. The pin is part of the "
                "command: under diff3 every span carries a base section, and "
                "every line number below it moves."
            )

    def keeping_head_reverts_the_base(self, name: str) -> bool:
        """Whether keeping this branch's content at `name` undoes a landed commit.

        True when the head's blob equals a merge base's AND the base side's blob
        differs: the head never edited the path, so the base side carries the only
        change and keeping the head's side drops it. `--all` because a criss-cross
        history has several bases. False when the path is absent from a base, and
        false when the base side matches the head too."""
        head_blob = self.blob_at(self.checked_out_head, name)
        if not head_blob or self.blob_at(self.merge_base_side, name) == head_blob:
            return False
        bases = git_lines(
            "merge-base", "--all", self.checked_out_head, self.merge_base_side
        )
        return any(self.blob_at(base, name) == head_blob for base in bases)

    def blob_at(self, ref: str, name: str) -> str:
        """The blob id `ref` records for `name`, empty when it records none."""
        return git("rev-parse", "-q", "--verify", f"{ref}:{name}", check=False).strip()
