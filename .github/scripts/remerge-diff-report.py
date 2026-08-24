#!/usr/bin/env python3
"""Render a markdown report of every hand-authored merge-resolution delta in a
PR's commit range, for supervision review.

A merge commit's tree is authored freely — nothing forces it to equal the
mechanical 3-way merge of its parents, so a conflict resolution can smuggle in
a change present in NEITHER parent (an "evil merge") that a normal one-parent
diff never shows. `git show --remerge-diff` reconstructs the mechanical merge
and diffs the recorded tree against it, isolating exactly what the resolver
typed. This script runs that over every merge commit in BASE_SHA..HEAD_SHA and
prints one markdown section per merge whose resolution still needs a human;
prints nothing when there is nothing hand-authored left to review.

"Still needs a human" is the whole job, because the raw delta over-reports
badly. Three filters retire a file or a hunk, each answering a question the
downstream reviewer cannot answer for itself:

  * SUPERSEDED — the file's bytes at head now equal the mechanical merge's or a
    parent's, so a later commit replaced the resolution's delta with reference
    bytes and nothing hand-authored ships.
  * TRACED — every block the hunk touches is already one parent's own edit
    against the parents' merge-base. That is the ordinary conflict resolution.
  * UNDONE — every trace of the hunk is gone from the file at head, so a
    follow-up commit corrected it.

What survives is a delta no parent's intent explains and no later commit undid.
The report also carries a per-file PROVENANCE block, because the downstream
reviewer has no shell and cannot read the parents itself: without it, a line a
branch removed deliberately and a line the resolver dropped look identical as a
`-`.

Env: BASE_SHA, HEAD_SHA (required). REMERGE_REPORT_MAX_BYTES caps the body;
UNSET MEANS NO CAP. Only the PR-comment renderer sets it, because only GitHub
imposes a size limit — the readers that actually audit have none, and a merge
dropped from what they read is a merge nobody looks at, on the one channel an
evil merge can hide in.

`--commit SHA` reports that one merge, uncapped, and reads no environment: it
serves a caller judging a resolution it just built. Head is that same commit
there, so nothing is superseded and no hunk is undone — the only correct answer
for a resolution that has not been pushed yet.

`--shas-out FILE` writes the merge SHAs the report covered, one per line, so a
consumer can key state on which merges a review actually read rather than
re-parsing the markdown.

Fails loud (SystemExit) on a merge with more than two parents: --remerge-diff
cannot reconstruct an octopus merge, and silently skipping one would report
"nothing to review" about exactly the kind of commit that needs review.
"""

import argparse
import functools
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, NamedTuple

MARKER = "<!-- remerge-diff-report -->"

_CONFLICT_MARKER = re.compile(r"(?:<{7}|={7}|>{7}|\|{7})")

_PROVENANCE_MAX_COMMITS = 10
PROVENANCE_OMITTED_NOTICE = "more commit(s) omitted from this side's list"
OMITTED_NOTICE = "omitted from THIS COMMENT to fit GitHub's size limit"

_INTRO = (
    f"{MARKER}\n"
    "## Hand-authored merge-resolution deltas\n\n"
    "Each section below is what a merge commit's resolution changed **on top "
    "of** the mechanical 3-way merge of its parents (`git show --remerge-diff "
    "<sha>`). This is the only place a conflict resolution can introduce "
    "content present in neither parent, so review these hunks as you would "
    "hand-written code — the ordinary PR diff does not isolate them.\n\n"
    "Deltas one parent's own commits explain, and deltas a later commit already "
    "undid, are filtered out. What remains is what no side's intent accounts "
    "for.\n"
)


@functools.lru_cache(maxsize=1)
def _repo_root() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
        # The invocation directory, NOT this script's own location. Both are
        # run from a trusted checkout AGAINST a different working tree — the
        # resolver runs out of ${RUNNER_TEMP}/resolver and reports on
        # $GITHUB_WORKSPACE — so a root derived from __file__ inspects the
        # wrong repository. An in-process caller must chdir before calling.
        cwd=Path.cwd(),
    ).stdout.strip()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True, cwd=_repo_root()
    ).stdout


def _fence(text: str) -> str:
    """A backtick fence strictly longer than any backtick run inside `text`,
    so PR-controlled diff content cannot break out of its data block."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _side_log(mb: str, tip: str, path: str) -> str:
    """The commits that touched `path` on one side since the parents' merge-base,
    capped with an explicit marker when more exist.

    The marker is what keeps this list from lying by omission: the reviewer reads
    "no commit on either side explains this hunk" as the evil-merge signal, so a
    silently truncated list MANUFACTURES that signal for a file with an ordinary
    busy history. `:(literal)` because a path is a path here, not a pattern —
    glob metacharacters in a filename would otherwise match something else, or
    nothing."""
    pathspec = f":(literal){path}"
    log = _git(
        "log",
        f"--max-count={_PROVENANCE_MAX_COMMITS + 1}",
        "--format=%h %s",
        f"{mb}..{tip}",
        "--",
        pathspec,
    ).strip()
    lines = log.split("\n") if log else []
    if len(lines) <= _PROVENANCE_MAX_COMMITS:
        return log
    total = int(_git("rev-list", "--count", f"{mb}..{tip}", "--", pathspec).strip())
    omitted = total - _PROVENANCE_MAX_COMMITS
    return "\n".join(
        [*lines[:_PROVENANCE_MAX_COMMITS], f"(…{omitted} {PROVENANCE_OMITTED_NOTICE})"]
    )


def _provenance(p1: str, p2: str, files: list[str]) -> str:
    """Which side of the merge changed each file the resolution touched.

    A reviewer reading only the delta cannot distinguish a resolution that took
    a side's DELIBERATE change from one that invented content, because neither
    parent is in front of it: a line the branch removed on purpose and a line
    the resolver dropped look identical as a `-`. These two logs are what
    separate them. A file only one side touched, resolved to that side, is the
    ordinary case; a hunk no side's commits explain is the evil-merge signal."""
    mb = _git("merge-base", p1, p2).strip()
    rows = []
    for path in sorted(files):
        sides = []
        for label, tip in (("parent 1", p1), ("parent 2", p2)):
            log = _side_log(mb, tip, path)
            # Backticks scrubbed for the same reason the merge subject's are:
            # commit subjects are PR-author text landing inside a fenced block.
            body = log.replace("`", "'") if log else "(untouched on this side)"
            sides.append(
                f"  {label}:\n" + "\n".join(f"    {ln}" for ln in body.split("\n"))
            )
        rows.append(f"{path}\n" + "\n".join(sides))
    text = "\n\n".join(rows)
    fence = _fence(text)
    return (
        "\n**Which side changed each file** (commits since the parents' "
        f"merge-base `{mb[:12]}`):\n\n{fence}\n{text}\n{fence}\n"
    )


def _tree_entry(rev: str, path: str) -> str | None:
    """The `ls-tree` entry — mode, type and oid — for `path` at `rev`, or None
    when the path is absent there.

    The mode is part of the identity: a resolution that only flips a file's
    executable bit ships a real delta that comparing blob oids alone would call
    superseded. Routed through `_git` so an unexpected git failure raises instead
    of reading as "absent", which would compare equal to another failure and
    grant supersession."""
    return _git("ls-tree", rev, "--", f":(literal){path}").strip() or None


def _mechanical_tree(parent1: str, parent2: str) -> str:
    """The mechanical 3-way merge of two parents as a tree oid (conflicted paths
    keep their conflict markers embedded)."""
    res = subprocess.run(
        ["git", "merge-tree", "--write-tree", parent1, parent2],
        capture_output=True,
        text=True,
        check=False,
        cwd=_repo_root(),
    )
    tree = res.stdout.split("\n", 1)[0]
    # Exit 1 is git's conflicted-but-written verdict. Anything else — or no tree
    # on stdout — must fail loud: _tree_entry reads "absent in both" as equal, so
    # a garbage tree here would mark every delta superseded and silence the
    # reviewer on exactly the merge under review.
    if res.returncode not in (0, 1) or not tree:
        raise SystemExit(
            f"git merge-tree --write-tree {parent1} {parent2} failed: "
            f"{res.stderr.strip()}"
        )
    return tree


def _superseded_paths(
    parents: list[str], head: str, paths: list[str]
) -> dict[str, str]:
    """The `paths` whose bytes at `head` equal a trusted reference — the
    mechanical merge's, or either parent's — mapped to which one.

    Later commits replaced the resolution's delta with reference bytes, so
    nothing hand-authored ships. Equality to a parent is sound for the same
    reason the mechanical case is: this reviewer guards the neither-parent
    channel, and bytes identical to a parent contain no neither-parent content by
    definition. A conflicted file can never match the mechanical blob — it embeds
    conflict markers — so the parent comparison is what makes a corrected
    conflict resolution supersedable at all."""
    mech = _mechanical_tree(parents[0], parents[1])
    parent_refs = [
        (parents[0], f"its first parent's ({parents[0][:12]}) exact bytes"),
        (parents[1], f"its second parent's ({parents[1][:12]}) exact bytes"),
    ]
    out: dict[str, str] = {}
    for p in paths:
        at_head = _tree_entry(head, p)
        # Absence matches only the MECHANICAL reference: a path missing at head
        # and missing from the mechanical merge means head agrees with the
        # mechanical result, so nothing hand-authored ships either way.
        if at_head == _tree_entry(mech, p):
            out[p] = "the mechanical merge's exact bytes"
            continue
        # Against a PARENT both entries must be PRESENT. _tree_entry returns None
        # for an absent path and None == None, so without this refusal a
        # resolution that DELETED a file one parent carried — a guardrail dropped
        # through a conflict resolution — would read as superseded by the parent
        # that never had it and vanish from the report, which is precisely the
        # delta that must stay visible.
        if at_head is None:
            continue
        for rev, source in parent_refs:
            if at_head == _tree_entry(rev, p):
                out[p] = source
                break
    return out


def _blob(rev: str, path: str) -> str:
    """The file's text at `rev` — empty when the path is absent there, which
    reads as "none of this delta's removals came back", so a resolution that
    deleted something and left it deleted stays under review. Decoding replaces
    undecodable bytes rather than raising: a replaced character can only fail a
    block match, which keeps the hunk in the report."""
    res = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    return res.stdout if res.returncode == 0 else ""


def _line_runs(hunk: str, sign: str) -> list[str]:
    """The hunk's maximal runs of consecutive `sign`-prefixed lines, each joined
    back into the multi-line block it was, with the sign stripped.

    Runs, not single lines, because a block of several lines is what makes "is
    this still in the file" a meaningful question — one short line can match
    anywhere.

    A conflict marker BREAKS a run and is never part of one. The mechanical merge
    of a conflicted file embeds `<<<<<<<`/`=======`/`>>>>>>>`, so they arrive
    here as `-` lines sitting between the two sides' text; joining across one
    would build a block that exists in no file anywhere and could therefore never
    be matched against a real blob."""
    lines = hunk.split("\n")[1:]  # [1:] drops the @@ header itself
    runs: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line.startswith(sign) or _CONFLICT_MARKER.match(line[1:]):
            if current:
                runs.append("\n".join(current))
                current = []
            continue
        current.append(line[1:])
    if current:
        runs.append("\n".join(current))
    return runs


def _count_block(text: str, block: str) -> int:
    """How many times `block` occurs in `text` as whole consecutive lines.

    A COUNT, not a membership test, and that is the load-bearing part: a file
    holding two identical lines would answer "still there" to a membership
    question after the resolution deleted one of them, retiring a deletion that
    ships. Counting also makes the comparison a no-op when the two texts are the
    same blob, which is what `--commit` mode passes."""
    lines = text.split("\n")
    needle = block.split("\n")
    return sum(
        1
        for i in range(len(lines) - len(needle) + 1)
        if lines[i : i + len(needle)] == needle
    )


def _anchored_runs(hunk: str, sign: str) -> list[str]:
    """{@link _line_runs}, each run prefixed by the line it FOLLOWS in the image
    its sign belongs to — the post-image for `+`, the pre-image for `-`.

    The anchor makes the parent comparison POSITIONAL. Counting the run alone
    asks whether the text appears more often in a parent than in the base, which
    is location-agnostic: a parent that added `foo()` at line 500 retires a
    resolution that inserted `foo()` at line 200, and that evil merge clears
    with no human reading it.

    Markers stay in the image so a run still BREAKS on one. Filtering them out
    splices the two runs a marker separates into one block, which is the
    weakening this instrument's header names first.
    """
    lines = hunk.split("\n")[1:]  # [1:] drops the @@ header itself
    # Markers stay IN the image so a run still breaks on one. Filtering them out
    # would splice the two runs a marker separates into one block, which is the
    # weakening this module's header names first: a joined block traces where
    # neither run does, and the hunk retires.
    image = [line for line in lines if line[:1] in (" ", sign)]
    runs: list[str] = []
    current: list[str] = []
    for index, line in enumerate(image):
        if not line.startswith(sign) or _CONFLICT_MARKER.match(line[1:]):
            if current:
                runs.append(_anchor_after(image, index - len(current) - 1, current))
                current = []
            continue
        current.append(line[1:])
    if current:
        runs.append(_anchor_after(image, len(image) - len(current) - 1, current))
    return runs


def _anchor_after(image: list[str], at: int, run: list[str]) -> str | None:
    """RUN prefixed by IMAGE's line at AT, or None when that neighbour cannot
    anchor it — the run opens the image, or a conflict marker sits there.

    None, never the bare run: a bare block IS the location-agnostic comparison
    the anchor replaces, so falling back to one retires exactly the runs with no
    context to check. An un-anchorable run never traces.
    """
    if at < 0 or _CONFLICT_MARKER.match(image[at][1:]):
        return None
    return "\n".join([image[at][1:], *run])


class ParentBlobs(NamedTuple):
    """One merge's three reference texts for a single file: the parents' common
    ancestor, and each parent."""

    base: str
    parent1: str
    parent2: str


def _one_parent_edited(
    blobs: ParentBlobs, bare: str, anchored: str | None, *, added: bool
) -> bool:
    """Did ONE parent make this edit, at a site both texts identify UNIQUELY?

    Counting alone cannot answer "at this place", and three separate leaks
    proved it: a parent that added the text elsewhere, a parent that merely
    deleted the line before it, and a parent whose two unrelated edits each
    satisfied one half. Each was a way for two count increases to come from two
    different sites.

    So the site must be unambiguous, and refusal is the answer when it is not.
    The run and its anchored form must each occur EXACTLY once in the parent and
    at most once in the base: one occurrence is one site, and the two counts then
    have nowhere else to come from. `A / X / A / Y` refuses because the anchor
    `A` is ambiguous; a parent holding two `GUARD` refuses because the run is.

    An un-anchored run answers False; see {@link _anchor_after}.
    """
    if anchored is None:
        return False
    return any(
        _edited_uniquely(parent, sibling, blobs.base, bare, anchored, added=added)
        for parent, sibling in (
            (blobs.parent1, blobs.parent2),
            (blobs.parent2, blobs.parent1),
        )
    )


def _same_predecessor(holder: str, other: str, anchor: str) -> bool:
    """Does ANCHOR follow the same line in both texts?

    Only asked when the anchor occurs in both, so a move shows up as a changed
    neighbour. An anchor absent from OTHER came with the edit and has no
    predecessor to compare, which the caller already handles.
    """
    if _count_block(other, anchor) == 0:
        return True

    def before(text: str) -> str | None:
        lines = text.split("\n")
        index = lines.index(anchor) if anchor in lines else -1
        if index < 0:
            return None
        return lines[index - 1] if index else ""

    return before(holder) == before(other)


def _edited_uniquely(
    parent: str, sibling: str, base: str, bare: str, anchored: str, *, added: bool
) -> bool:
    """One parent's answer for {@link _one_parent_edited}, refusing ambiguity."""
    # The side that must hold the edit: the parent for an addition, the base for
    # a deletion. Exactly one occurrence there, so the site is a single place.
    holder, other = (parent, base) if added else (base, parent)
    for block in (bare, anchored):
        if _count_block(holder, block) != 1 or _count_block(other, block) != 0:
            return False
    # The ANCHOR LINE too, on BOTH sides, and this is what the block counts miss:
    # with base `A / X / A / Y` a parent can edit after the SECOND `A` while the
    # resolution edits after the FIRST, and both blocks stay unique. Zero on the
    # other side is fine — the parent brought the anchor with the run, so no
    # earlier occurrence competes with it.
    anchor_line = anchored.split("\n")[0]
    if anchor_line == bare:
        return True
    if _count_block(holder, anchor_line) != 1 or _count_block(other, anchor_line) > 1:
        return False
    # The anchor must also still be in the SAME PLACE. Counts cannot tell an
    # anchor that stayed and gained a line from one the parent MOVED and gained
    # a line at its new home: with base `A / X / Y` and parent `X / Y / A /
    # GUARD`, every count above is 1, and retiring the hunk clears an insertion
    # the parent made somewhere else. The line before it answers that cheaply.
    if not _same_predecessor(holder, other, anchor_line):
        return False
    # An anchor the base never held came with this edit — unless the SIBLING
    # introduced one too, and then two parents put the same anchor at two sites
    # and the hunk names neither.
    return (
        _count_block(other, anchor_line) == 1 or _count_block(sibling, anchor_line) == 0
    )


def _hunk_traced_to_the_parents(hunk: str, blobs: ParentBlobs) -> bool:
    """Is every block this hunk touches one parent's own edit against the
    merge-base, AT THIS PLACE — each removed block deleted by that parent, each
    added block added by it?

    This is the question the reviewer is asked — "does one side's intent explain
    this hunk?" — answered from the three blobs rather than from a list of
    commit subjects, which is all the reviewer gets. Answering it here is what
    lets an ordinary resolution clear with no human reading it.

    The comparison is directional, and that direction is the safety argument: a
    guard one parent ADDED and the resolution DELETED has a base count of zero,
    so `0 > 0` fails and the hunk stays under review.

    Blocks are attributed independently — a hunk may follow one side's deletion
    and the other's addition. A hunk whose every signed line is a conflict
    marker yields no blocks and passes vacuously, which is correct: a marker is
    never valid file content.
    """
    return all(
        _one_parent_edited(blobs, bare, anchored, added=False)
        for bare, anchored in zip(_line_runs(hunk, "-"), _anchored_runs(hunk, "-"))
    ) and all(
        _one_parent_edited(blobs, bare, anchored, added=True)
        for bare, anchored in zip(_line_runs(hunk, "+"), _anchored_runs(hunk, "+"))
    )


def _hunk_undone_at_head(hunk: str, head_text: str, merge_text: str) -> bool:
    """Is every trace of this hunk's resolution gone from the file at `head` —
    each added block occurring FEWER times than in the merge's own blob, and
    each removed block MORE?

    Judging the delta's content gives a correcting commit somewhere to land on a
    branch that keeps moving: the fixer restores what the resolution dropped and
    this hunk leaves the review. A pushed merge's remerge-diff never changes, so
    a follow-up commit is the only correction available.

    Counts here are UNANCHORED, unlike {@link _hunk_traced_to_the_parents}'s:
    this compares two versions of the SAME branch, where a correcting commit is
    free to have moved the text.

    Both halves are required, so a partly-undone hunk stays in the report.
    """
    added = _line_runs(hunk, "+")
    removed = _line_runs(hunk, "-")
    if not added and not removed:
        return False
    return all(_added_gone_at_head(b, head_text, merge_text) for b in added) and all(
        _count_block(head_text, b) > _count_block(merge_text, b) for b in removed
    )


def _line_gone_at_head(line: str, head_text: str) -> bool:
    """No occurrence of this exact line at head? A blank line always answers False."""
    return bool(line.strip()) and _count_block(head_text, line) == 0


def _added_gone_at_head(block: str, head_text: str, merge_text: str) -> bool:
    """Does the head carry no trace of this added block — fewer occurrences than
    the merge left, AND every content line absent outright (blanks exempt)?

    The per-line half is load-bearing. `_line_runs` joins consecutive added lines
    into ONE block, so a resolution that adds a comment above a smuggled
    guard-removal makes them a single unit; a later commit that merely REWORDS
    the comment drops the block's count to zero while the smuggled line still
    ships, and the still-shipping delta leaves the report unread.

    Absence OUTRIGHT, never "fewer times than the merge left it": a resolution
    that adds the smuggled line TWICE and later deletes one copy makes the count
    fall while a copy still ships. The accepted cost is that a block holding a
    line occurring anywhere else at head (`fi`, `}`, `done`) can never retire the
    whole hunk.
    """
    if _count_block(head_text, block) >= _count_block(merge_text, block):
        return False
    return all(
        _line_gone_at_head(line, head_text) or not line.strip()
        for line in block.split("\n")
    )


def _drop_hunks(file_diff: str, retire: Callable[[str], bool]) -> tuple[str, int]:
    """`file_diff` with every hunk `retire` accepts removed, and how many went.

    The file header survives its hunks whenever it carries a MODE change: a
    content read cannot judge an executable-bit flip, so dropping the header
    would hide an un-executabled guard. A diff with no hunks at all (a mode-only
    delta) is returned untouched for the same reason."""
    starts = [m.start() for m in re.finditer(r"(?m)^@@ .*$", file_diff)]
    if not starts:
        return file_diff, 0
    bounds = [*starts, len(file_diff)]
    hunks = [file_diff[bounds[i] : bounds[i + 1]] for i in range(len(starts))]
    kept = [h for h in hunks if not retire(h)]
    header = file_diff[: starts[0]]
    if not kept and "\nnew mode " not in f"\n{header}":
        return "", len(hunks)
    return header + "".join(kept), len(hunks) - len(kept)


def _split_by_file(diff: str) -> list[tuple[str, str]]:
    """`(path, that file's diff)` for each `diff --git` section.

    The path comes from the `+++ b/` line rather than the `diff --git` header:
    the header repeats a possibly-quoted path twice, while `+++` carries it once.
    A pure deletion has no `+++ b/`, so it falls back to `--- a/`; a section with
    neither is yielded under an empty path so its content is still reported
    rather than silently dropped."""
    starts = [m.start() for m in re.finditer(r"(?m)^diff --git ", diff)]
    if not starts:
        return []
    bounds = [*starts, len(diff)]
    out = []
    for i in range(len(starts)):
        section = diff[bounds[i] : bounds[i + 1]]
        plus = re.search(r"(?m)^\+\+\+ b/(?P<path>.*)$", section)
        minus = re.search(r"(?m)^--- a/(?P<path>.*)$", section)
        path = ""
        if plus and plus.group("path") != "/dev/null":
            path = plus.group("path")
        elif minus and minus.group("path") != "/dev/null":
            path = minus.group("path")
        out.append((path, section))
    return out


def _unmergeable(paths: list[str], source: str | None) -> frozenset[str]:
    """Which of `paths` `.gitattributes` marks `-merge`, reading the attributes
    at `source`, or in the checkout when it is None."""
    at = [f"--source={source}"] if source else []
    # `-z` writes <path> NUL <attribute> NUL <value> NUL per path, so the split
    # ends in one empty field; dropping it makes the triples exact.
    fields = _git("check-attr", *at, "-z", "merge", "--", *paths).split("\0")[:-1]
    triples = zip(fields[::3], fields[2::3], strict=True)
    return frozenset(path for path, value in triples if value == "unset")


def _safe_path(path: str) -> str:
    """A path fit for a note OUTSIDE a diff fence: whitespace collapsed and
    backticks stripped, so a PR-authored name cannot close the code span and
    land the rest of itself as live markdown."""
    return re.sub(r"\s", " ", path).replace("`", "'")


def _merged_tree_derived(paths: list[str], merge: str, head: str) -> frozenset[str]:
    """Which of `paths` `.gitattributes` marks `-merge` — a derived artifact
    (a lockfile, a generated table) git must never line-merge.

    Its one correct content is what the MERGED tree fixes, so no per-hunk read
    of the parents can certify it: tracing answers each hunk ALONE, and hunks
    that each match one parent still combine into bytes no generator produces.
    Those files therefore keep every hunk, and the reviewer is asked for the
    whole-file check instead.

    The attribute is read in three trees and the answers are unioned: the
    checkout this runs in, the `merge` itself, and `head`. A rule the PR
    declares is absent from the base checkout, and one the resolution declares
    and a later commit drops is absent from the head too. A tree the PR
    controls only ever ADDS a file here, so it raises the scrutiny and never
    lowers it.
    """
    if not paths:
        return frozenset()
    at_trees = [_unmergeable(paths, rev) for rev in (merge, head)]
    return _unmergeable(paths, None).union(*at_trees)


def _derived_note(paths: list[str], derived: frozenset[str]) -> str:
    """The line naming every kept path whose content only the merged tree fixes."""
    named = sorted(set(paths) & derived)
    if not named:
        return ""
    listed = ", ".join(f"`{_safe_path(p)}`" for p in named)
    return (
        f"**Derived from the merged tree:** {listed} — git is told never to "
        "line-merge these (`-merge`), so no hunk of them is retired as traced to "
        "a parent. Judge each as a whole file, and ask for the check the "
        "instructions name; do not give it a line-by-line verdict.\n"
    )


def _surviving_diff(sha: str, parents: list[str], at_head: str, diff: str):
    """The parts of `diff` no filter retires, how many files and hunks went, and
    the paths only the merged tree fixes.

    Returns `(kept, retired, derived)` where `kept` is `(path, its diff)`."""
    files = _split_by_file(diff)
    diff_paths = [p for p, _ in files if p]
    derived = _merged_tree_derived(diff_paths, sha, at_head)
    # A derived file's one correct content is what the MERGED tree fixes, so
    # bytes equal to a parent's are staleness, not evidence — one side's file
    # beside the other side's number. Supersession cannot certify it either.
    superseded = {
        p: why
        for p, why in _superseded_paths(parents, at_head, diff_paths).items()
        if p not in derived
    }
    mb = _git("merge-base", parents[0], parents[1]).strip()
    kept: list[tuple[str, str]] = []
    retired = 0
    for path, file_diff in files:
        if path in superseded:
            retired += 1
            continue
        blobs = ParentBlobs(
            base=_blob(mb, path),
            parent1=_blob(parents[0], path),
            parent2=_blob(parents[1], path),
        )
        merge_text = _blob(sha, path)
        head_text = _blob(at_head, path)

        traceable = path not in derived

        def retire(
            hunk: str,
            blobs=blobs,
            head=head_text,
            merged=merge_text,
            traceable=traceable,
        ) -> bool:
            return (
                traceable and _hunk_traced_to_the_parents(hunk, blobs)
            ) or _hunk_undone_at_head(hunk, head, merged)

        remaining, dropped = _drop_hunks(file_diff, retire)
        retired += dropped
        if remaining.strip():
            kept.append((path, remaining))
    return kept, retired, derived


def _section(sha: str, head: str | None) -> str:
    """The report section for one merge commit: empty when nothing hand-authored
    survives the three filters."""
    parents = _git("rev-list", "--parents", "-n1", sha).split()[1:]
    if len(parents) > 2:
        raise SystemExit(
            f"merge {sha} has {len(parents)} parents; --remerge-diff cannot "
            "reconstruct an octopus merge, so its resolution cannot be reviewed "
            "this way. Re-merge as a chain of two-parent merges."
        )
    if len(parents) < 2:
        return ""
    diff = _git("show", "--remerge-diff", "--no-color", "--format=", sha)
    if not diff.strip():
        return ""

    kept, retired, derived = _surviving_diff(sha, parents, head or sha, diff)
    if not kept:
        return ""

    body = "".join(text for _, text in kept)
    subject = _git("log", "-1", "--format=%s", sha).strip().replace("`", "'")
    fence = _fence(body)
    lines = body.strip().count("\n") + 1
    note = f" — {retired} explained by a parent or already undone" if retired else ""
    kept_paths = [p for p, _ in kept if p]
    provenance = _provenance(parents[0], parents[1], kept_paths)
    derived_note = _derived_note(kept_paths, derived)
    # Collapsed by default: these deltas are often long, and a report with
    # several merges would otherwise dominate the PR page. The summary keeps the
    # sha/subject/size visible so a reviewer can decide whether to expand. A
    # blank line after <summary> is required for GitHub to render the fenced
    # diff inside the <details>.
    return (
        f"\n<details><summary><code>{sha[:12]}</code> {subject} "
        f"({lines}-line delta{note})</summary>\n\n"
        f"{derived_note}{provenance}\n{fence}diff\n{body.rstrip()}\n{fence}\n\n</details>\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Render merge-resolution deltas.")
    parser.add_argument(
        "--commit",
        help="report only this merge commit, uncapped, instead of every merge in "
        "BASE_SHA..HEAD_SHA",
    )
    parser.add_argument(
        "--shas-out",
        help="write the merge SHAs this report covered, one per line, so a "
        "consumer can key state on which merges were read",
    )
    args = parser.parse_args()
    if args.commit:
        merges, head, max_bytes = [args.commit], None, None
    else:
        base, head = os.environ["BASE_SHA"], os.environ["HEAD_SHA"]
        merges = list(reversed(_git("rev-list", "--merges", f"{base}..{head}").split()))
        cap = os.environ.get("REMERGE_REPORT_MAX_BYTES")
        # UNSET MEANS NO CAP. Only the PR-comment renderer sets one, because only
        # GitHub imposes a size limit; the readers that audit have none, and a
        # merge dropped from what they read is a merge nobody looks at.
        max_bytes = int(cap) if cap else None

    sections = [(sha, _section(sha, head)) for sha in merges]
    sections = [(sha, text) for sha, text in sections if text]
    if args.shas_out:
        with open(args.shas_out, "w", encoding="utf-8") as fh:
            fh.write("".join(f"{sha}\n" for sha, _ in sections))
    if not sections:
        return
    # Truncate at section boundaries, never mid-fence: a cut inside a fenced diff
    # would leave the fence open and render the notice as diff content.
    report, dropped = _INTRO, []
    for sha, text in sections:
        if max_bytes is not None and len((report + text).encode()) > max_bytes:
            dropped.append(sha[:12])
        else:
            report += text
    if dropped:
        report += (
            f"\n**…{len(dropped)} merge(s) {OMITTED_NOTICE} "
            f"({', '.join(f'`{sha}`' for sha in dropped)}) — run "
            "`git show --remerge-diff <sha>` locally for those deltas.**\n"
        )
    print(report)


if __name__ == "__main__":
    main()
