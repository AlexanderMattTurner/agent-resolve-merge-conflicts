#!/usr/bin/env python3
"""Render a markdown report of every hand-authored merge-resolution delta in a
PR's commit range, for supervision review.

A merge commit's tree is authored freely, so a conflict resolution can smuggle
in a change present in NEITHER parent (an "evil merge") that a normal
one-parent diff never shows. `git show --remerge-diff` reconstructs the
mechanical merge and diffs the recorded tree against it, isolating exactly
what the resolver typed. This runs that over every merge commit in
BASE_SHA..HEAD_SHA and prints one section per merge whose resolution differs
from the mechanical result.

Three classes of delta are annotated instead of rendered, since a provenance
read of them cannot produce a finding anyone can act on:
  - one made ENTIRELY of the parents' own edits (`hunk_traced_to_the_parents`);
  - one the HEAD has already undone, whole-file (`_superseded_paths`) or hunk
    by hunk (`hunk_undone_at_head`);
  - a GENERATOR-OWNED output (the caller's rule table, named by
    AUTO_RESOLVE_RESOLVER_MJS), whose bytes a required check re-derives from
    source on the PR head.
Lockfiles are NOT in that set.

A rendered section also carries git's own conflict notices for paths the
mechanical merge could not resolve at all (`_notice_lines`).

Env and argv:
  - BASE_SHA, HEAD_SHA — required.
  - REMERGE_REPORT_MAX_BYTES caps the body. UNSET MEANS NO CAP; only the
    PR-comment renderer sets it (GitHub truncates a comment at 65536).
  - `--commit SHA` reports that one merge instead, uncapped.
  - `--shas-out PATH` additionally writes the sha of every merge rendered, so a
    consumer can key durable state on which merges a review covered. Refuses to
    run under a cap.

Fails loud on a merge with more than two parents: --remerge-diff cannot
reconstruct an octopus merge.

`.claude/dev-notes` § "Merge-resolution delta report: annotation classes and refusals (`.github/resolver/remerge-diff-report.py`)" carries the rest.
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "auto-resolve"))

# pylint: disable=wrong-import-position  # must follow the sys.path insert above
from _lockfiles import LockfileError  # noqa: E402
from _lockfiles import regenerate as regenerate_lockfile  # noqa: E402
from _lockfiles import rule_for as lockfile_rule_for  # noqa: E402
from _conflict_hunks import MECHANICAL_CONFLICT_STYLE, conflict_style_args  # noqa: E402
from _git_io import bind_repo  # noqa: E402
from _merge_attr import MergePolicy, policies  # noqa: E402
from _owned import RESOLVER_ENV, Owned, load_from_env as caller_owned  # noqa: E402
from _shared_lock_entries import changed_shared_entries  # noqa: E402
from _merge_delta_novelty import (  # noqa: E402
    ParentBlobs,
    corrected_positions,
    forced_collisions,
    hunk_traced_to_the_parents,
    hunk_undone_at_head,
    relocated_positions,
)

MARKER = "<!-- remerge-diff-report -->"

# The notice a size-capped render emits for the merges it left out.
OMITTED_NOTICE = "omitted from THIS COMMENT to fit GitHub's size limit"

_INTRO = (
    f"{MARKER}\n"
    "## Hand-authored merge-resolution deltas\n\n"
    "Each section below is what a merge commit's resolution changed **on top "
    "of** the mechanical 3-way merge of its parents (`git show --remerge-diff "
    "<sha>`). This is the only place a conflict resolution can introduce "
    "content present in neither parent, so review these hunks as you would "
    "hand-written code — the ordinary PR diff does not isolate them.\n"
)


def _capture(*command: str) -> str:
    """`command`'s stdout, or a SystemExit naming the command, its exit status
    and its own stderr. Not `check=True`, whose CalledProcessError message
    discards the subprocess's stderr.
    """
    res = subprocess.run(command, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise SystemExit(
            f"{shlex.join(command)} failed (exit {res.returncode}): "
            f"{res.stderr.strip() or '<no stderr>'}"
        )
    return res.stdout


def _git(*args: str) -> str:
    return _capture("git", *args)


def _fence(text: str) -> str:
    """A backtick fence longer than any run inside `text`, so PR-controlled diff content
    cannot break out of its data block."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


# Per side, per file — the handful of commits that touched it, not the
# branch's whole history.
_PROVENANCE_MAX_COMMITS = 10

# What a side's commit list says when the cap cut it short.
PROVENANCE_OMITTED_NOTICE = "more commit(s) omitted from this side's list"


def _side_log(mb: str, tip: str, path: str) -> str:
    """Commits touching `path` on one side, capped at _PROVENANCE_MAX_COMMITS
    with a marker when more exist, or truncation would fake the evil-merge signal."""
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
        [
            *lines[:_PROVENANCE_MAX_COMMITS],
            f"(…{omitted} {PROVENANCE_OMITTED_NOTICE})",
        ]
    )


def _provenance(p1: str, p2: str, files: list[str]) -> str:
    """Which side of the merge changed each file the resolution touched: a
    file only one side touched is the ordinary case, a hunk no side's commits
    explain is the evil-merge signal."""
    mb = _git("merge-base", p1, p2).strip()
    rows = []
    for path in sorted(files):
        sides = []
        for label, tip in (("parent 1", p1), ("parent 2", p2)):
            log = _side_log(mb, tip, path)
            # Backticks scrubbed: commit subjects are PR-author text in a fence.
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


@cache
def _generated_paths() -> frozenset[str]:
    """What a required check re-derives from source, from the CALLING repository's
    rule table (`--owned --rederived-only`). That re-derivation is the whole reason
    a delta to one of these may be annotated away instead of read, so a rule that
    does not claim it stays in the review. A rule's owned DIRECTORY is dropped
    here: this set is compared against exact delta paths.

    `_owned.RESOLVER_ENV` names that table as an ABSOLUTE path inside the trusted
    base checkout. This reviewer runs with the untrusted PR head as its cwd, and
    the table decides which deltas it stops reading, so a relative path would let
    the pull request declare its own evil merge generator-owned.
    """
    return caller_owned("--rederived-only").exact


@cache
def _caller_owned_paths() -> Owned:
    """Every path AND directory the calling repository's rule table declares."""
    return caller_owned()


@cache
def _regenerable_paths() -> frozenset[str]:
    """The rule-owned outputs NO required check re-derives — the lockfile class
    within the CALLER's own declared rules.

    These are exactly what `_generated_paths` excludes, so they reach the reviewer
    as if a hand wrote them. A regenerated lockfile then reads as an evil merge:
    it holds content in neither parent BY CONSTRUCTION. `_verified_regenerated`
    below answers that mechanically instead of asking a model to. This is one of
    TWO regenerable classes `_verified_regenerated` covers — the other is a
    lockfile the caller declares no rule for at all, which
    `_resolver_builtin_lockfile_paths` names instead."""
    return _caller_owned_paths().exact - _generated_paths()


def _resolver_builtin_lockfile_paths(paths: list[str]) -> frozenset[str]:
    """Of `paths`, the ones this resolver's own registry (`_lockfiles.py`)
    recognizes AND the caller declares no rule for. A caller-owned path is
    excluded so the caller's rule table stays the one authority over its own
    lockfiles — the same precedence `_owned.Owned.covers` enforces
    everywhere else this registry is consulted."""
    owned = _caller_owned_paths()
    return frozenset(p for p in paths if lockfile_rule_for(p) and not owned.covers(p))


class RegenCheck(NamedTuple):
    """What re-running the generators said about the rule-owned outputs no
    required check re-derives."""

    verified: dict[str, str]
    mismatched: list[str]


def _diff_quiet(tree: str, path: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", tree, "diff", "--quiet", "--", f":(literal){path}"],
            check=False,
        ).returncode
        == 0
    )


def _verify_via_caller_rules(tree: str, rules: str, candidates: list[str]) -> list[str]:
    """Run the caller's own regeneration table once over TREE and return which
    of CANDIDATES it reproduced. [] (with a warning) when the table itself
    fails — every candidate then stays unreproduced, which is the fail-toward-
    review direction this whole check must never leave."""
    if not rules or not candidates:
        return []
    done = subprocess.run(
        ["node", rules, f"--root={tree}"], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        sys.stderr.write(
            f"::warning::the regeneration check exited {done.returncode}, so "
            f"{len(candidates)} rule-owned output(s) stay in the review: "
            f"{done.stderr.strip() or '<no stderr>'}\n"
        )
        return []
    return [path for path in candidates if _diff_quiet(tree, path)]


def _verify_via_builtin_registry(tree: str, candidates: list[str]) -> list[str]:
    """Run this resolver's own lockfile registry over each of CANDIDATES
    individually and return which it reproduced. A candidate whose regeneration
    raises (missing tool, unreadable manifest) stays unreproduced rather than
    aborting the batch — one lockfile this cannot verify says nothing about
    the others."""
    reproduced = []
    for path in candidates:
        try:
            regenerate_lockfile(path, tree)
        except LockfileError as exc:
            sys.stderr.write(
                f"::warning::could not verify '{path}' against this resolver's own "
                f"registry, so it stays in the review: {exc}\n"
            )
            continue
        if _diff_quiet(tree, path):
            reproduced.append(path)
    return reproduced


def _verified_regenerated(sha: str, paths: list[str]) -> RegenCheck:
    """Of `paths`, the rule-owned outputs whose bytes at `sha` EQUAL a fresh
    re-derivation, mapped to the note that says so, plus the ones that differ.

    Re-derivation, never a claim: whichever step wrote these bytes, they are the
    generator's own if running the generator again reproduces them. So this needs
    no provenance channel from the resolve job, and no such channel could make
    this reviewer more permissive.

    TWO generators can own a candidate: the caller's own rule table
    (`_regenerable_paths`), or this resolver's built-in lockfile registry for a
    caller that declares no rule at all (`_resolver_builtin_lockfile_paths`) —
    the fallback #4585's fix exists to cover.

    Runs the generators, so it is opt-in through AUTO_RESOLVE_VERIFY_REGENERATED
    and off by default: a rule's command runs build backends the tree under review
    wrote. Every path this cannot verify — the flag unset, the generator missing,
    the bytes differing — STAYS in the review, which is the status quo this
    narrows and never widens.

    AUTO_RESOLVE_PRE_PASS_VERIFIED is the one exception to "never a claim":
    only bundle.py sets it, after its verify_generated_artifacts ran the same
    table's `--verify` over this same tree in this same job and failed the job
    on any mismatch. The post-push watchdog never sets it, so a pushed
    resolution is still re-derived rather than believed.
    """
    rules = os.environ.get(RESOLVER_ENV, "").strip()
    caller_candidates = [p for p in paths if p in _regenerable_paths()]
    builtin_candidates = sorted(_resolver_builtin_lockfile_paths(paths))
    if not caller_candidates and not builtin_candidates:
        return RegenCheck({}, [])
    if os.environ.get("AUTO_RESOLVE_VERIFY_REGENERATED") != "true":
        return RegenCheck({}, [])
    verified: dict[str, str] = {}
    if os.environ.get("AUTO_RESOLVE_PRE_PASS_VERIFIED") == "true":
        verified = {
            path: "this job's own pre-pass `--verify` post-condition already "
            "compared its committed bytes against a fresh generation"
            for path in caller_candidates
        }
        caller_candidates = []
    remaining = caller_candidates + builtin_candidates
    reproduced: list[str] = []
    if remaining:
        with tempfile.TemporaryDirectory(prefix="remerge-regen-") as scratch:
            tree = str(Path(scratch) / "tree")
            _git("worktree", "add", "--detach", "--quiet", tree, sha)
            try:
                reproduced = _verify_via_caller_rules(
                    tree, rules, caller_candidates
                ) + _verify_via_builtin_registry(tree, builtin_candidates)
            finally:
                _git("worktree", "remove", "--force", tree)
    verified.update(
        {
            path: "a fresh run of this repository's own generator reproduces its "
            "committed bytes exactly"
            for path in reproduced
        }
    )
    return RegenCheck(
        verified,
        [path for path in remaining if path not in reproduced],
    )


def _tree_entry(rev: str, path: str) -> str | None:
    """The `ls-tree` entry — mode, type and oid — for `path` at `rev`, or None
    when absent. The mode matters: an executable-bit-only flip is a real delta
    that comparing blob oids alone would call superseded.
    """
    return _git("ls-tree", rev, "--", f":(literal){path}").strip() or None


def _mechanical_tree(parent1: str, parent2: str) -> str:
    """The mechanical 3-way merge of two parents as a tree oid. Never cached on
    the parent shas: the oid names a loose object in the repository this ran in,
    and two repositories can hold the same commit sha for the same content."""
    res = subprocess.run(
        # cwd-git-ok: the repository under report IS the one this runs in — every
        # other read here resolves the same way, and --write-tree only adds loose
        # objects to it
        [
            "git",
            *conflict_style_args(MECHANICAL_CONFLICT_STYLE),
            "merge-tree",
            "--write-tree",
            parent1,
            parent2,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    tree = res.stdout.split("\n", 1)[0]
    # Exit 1 is git's conflicted-but-written verdict; anything else must fail
    # loud, or a garbage tree here would mark every delta superseded.
    if res.returncode not in (0, 1) or not tree:
        raise SystemExit(
            f"git merge-tree --write-tree {parent1} {parent2} failed: "
            f"{res.stderr.strip()}"
        )
    return tree


def _delta_paths(sha: str) -> list[str]:
    """Every file `sha`'s resolution changed on top of the mechanical merge."""
    listing = _git("show", "--remerge-diff", "--name-only", "-z", "--format=", sha)
    return [p for p in listing.split("\0") if p]


def _superseded_paths(
    parents: list[str], head: str, paths: list[str]
) -> dict[str, str]:
    """The `paths` of a merge's remerge-diff whose bytes at `head` equal a
    trusted reference — the mechanical merge's, or either parent's — mapped to
    a description of which. Bytes identical to a parent contain no
    neither-parent content by definition, since a conflicted file can never
    match the mechanical blob.
    """
    mech = _mechanical_tree(parents[1], parents[2])
    parent_refs = [
        (parents[1], f"its first parent's ({parents[1][:12]}) exact bytes"),
        (parents[2], f"its second parent's ({parents[2][:12]}) exact bytes"),
    ]
    out: dict[str, str] = {}
    for p in paths:
        at_head = _tree_entry(head, p)
        # Absence matches only the MECHANICAL reference: missing at head and
        # missing from the mechanical merge means head agrees with it.
        if at_head == _tree_entry(mech, p):
            out[p] = "the mechanical merge's exact bytes"
            continue
        # Against a PARENT both entries must be PRESENT, or a resolution that
        # DELETED a file one parent carried would read as superseded by the
        # parent that never had it, hiding the delta that must stay visible.
        if at_head is None:
            continue
        for rev, source in parent_refs:
            if at_head == _tree_entry(rev, p):
                out[p] = source
                break
    return out


def _blob(rev: str, path: str) -> str:
    """The file's text at `rev` — empty when the path is absent there, so a
    deletion left deleted stays under review. Undecodable bytes are replaced,
    not raised: a replaced character can only fail a block match.
    """
    res = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    return res.stdout if res.returncode == 0 else ""


def _hunks(file_diff: str) -> tuple[list[int], list[str]]:
    """Where each hunk of `file_diff` starts, and the hunk text from each start
    to the next. An empty start list means a mode-only delta with no hunk.
    """
    starts = [m.start() for m in re.finditer(r"(?m)^@@ .*$", file_diff)]
    bounds = [*starts, len(file_diff)]
    return starts, [file_diff[bounds[i] : bounds[i + 1]] for i in range(len(starts))]


def _drop_hunks(file_diff: str, retire: Callable[[str], bool]) -> tuple[str, int]:
    """`file_diff` with every hunk `retire` accepts removed, and how many were
    removed. The header survives when it carries a MODE change. A mode-only
    delta (no hunks) is returned untouched.
    """
    starts, hunks = _hunks(file_diff)
    if not starts:
        return file_diff, 0
    kept = [h for h in hunks if not retire(h)]
    header = file_diff[: starts[0]]
    if not kept and "\nnew mode " not in f"\n{header}":
        return "", len(hunks)
    return header + "".join(kept), len(hunks) - len(kept)


class SectionSplit(NamedTuple):
    """The two things a section of `git show --remerge-diff` can be."""

    deltas: list[tuple[str, str]]
    """`(path, its remerge-diff)` for every section that carries diff content."""
    notices: list[str]
    """git's own `remerge …` lines from the sections that carry no diff content."""


def _notice_lines(section: str) -> list[str] | None:
    """The `remerge …` lines of a section that carries NO diff content, or
    None when it has content and must be attributed to a delta path. Git puts
    each notice on its own `remerge `-prefixed line, and no diff content line
    can start that way.
    """
    body = [line for line in section.split("\n")[1:] if line]
    if not body or not all(line.startswith("remerge ") for line in body):
        return None
    return body


def _conflict_notice_note(notices: list[str]) -> list[str]:
    """The report lines for the paths the mechanical merge could not resolve —
    reported, never dropped, since git could not merge there. Fenced since the
    branch names and commit subjects inside are PR-author text.
    """
    if not notices:
        return []
    text = "\n".join(notices)
    fence = _fence(text)
    return [
        "**Paths the mechanical merge could not resolve** — git reports these "
        "conflicts with no content delta of their own, so this report has no hunk "
        "to judge for them. Each line names the path and the kind of conflict:",
        "",
        f"{fence}\n{text}\n{fence}",
        "",
    ]


@cache
def _quoted_delta_paths(sha: str) -> list[str]:
    """`_delta_paths(sha)` again, as git SPELLS each path in a `diff --git`
    header, in the same order. Cached: each call reconstructs the whole
    mechanical merge, so K escaped sections would otherwise pay K tree merges.
    `core.quotePath=false` matches the render's own setting.
    """
    return _git(
        "-c",
        "core.quotePath=false",
        "show",
        "--remerge-diff",
        "--name-only",
        "--format=",
        sha,
    ).splitlines()


def _names_an_annotated_escaped_path(
    sha: str, paths: list[str], annotated: list[str], first_line: str
) -> bool:
    """Whether `first_line` is the section of an ANNOTATED path git escaped.

    Git wraps a header in C-style quotes for a path holding a quote, backslash
    or control character, so such a section never matches the raw path
    `--name-only -z` reported. The escaped spelling comes from
    :func:`_quoted_delta_paths`, paired positionally with the raw one.
    """
    quoted = _quoted_delta_paths(sha)
    # strict=True guards against a mis-paired listing dropping the wrong section.
    return any(
        first_line.endswith(f' "b/{display[1:]}')
        for raw, display in zip(paths, quoted, strict=True)
        if raw in annotated and display.startswith('"')
    )


def _reviewable_diffs(sha: str, paths: list[str], annotated: list[str]) -> SectionSplit:
    """Every remerge-diff section of `sha`, split into the file deltas not
    annotated away and git's own content-free conflict notices. ONE `git show
    --remerge-diff` for the whole merge, not one per path. No pathspec narrows
    it, and the annotated paths drop out AFTER attribution, since excluding a
    rename's DESTINATION would strand its source as an unattributable
    deletion. Each content section is attributed by matching its `diff --git
    a/… b/<dest>` line against the delta paths, longest first, or fails loud.
    """
    # quotePath=false so a non-ASCII path matches the `--name-only -z` bytes.
    full = _git(
        "-c",
        "core.quotePath=false",
        "show",
        "--remerge-diff",
        "--no-color",
        "--format=",
        sha,
    )
    starts = [m.start() for m in re.finditer(r"(?m)^diff --git ", full)]
    bounds = [*starts, len(full)]
    deltas, notices = [], []
    for i, start in enumerate(starts):
        section = full[start : bounds[i + 1]]
        notice = _notice_lines(section)
        if notice is not None:
            notices += notice
            continue
        first_line = section.split("\n", 1)[0]
        match = next(
            (
                p
                for p in sorted(paths, key=len, reverse=True)
                if first_line.endswith(f" b/{p}")
            ),
            None,
        )
        if match is None:
            if _names_an_annotated_escaped_path(sha, paths, annotated, first_line):
                continue
            raise SystemExit(
                f"merge {sha}: cannot attribute the remerge-diff section "
                f"{first_line!r} to any of its changed paths, so the head bytes to "
                "judge it against are unknown. Refusing to render a delta this "
                "report might attribute to the wrong file."
            )
        if match in annotated:
            continue
        deltas.append((match, section))
    return SectionSplit(deltas, notices)


def _unmergeable(paths: list[str], source: str | None) -> frozenset[str]:
    """Which of `paths` the repository forbids git to merge, reading SOURCE's
    attributes, or the checkout's when SOURCE is None."""
    return frozenset(
        path
        for path, policy in policies(paths, source=source).items()
        if policy is MergePolicy.UNMERGEABLE
    )


def _merged_tree_derived(paths: list[str], merge: str, head: str) -> frozenset[str]:
    """Which of `paths` `.gitattributes` marks `-merge` — a derived artifact git must never line-merge.

    Its one correct content is what the MERGED tree fixes, so no per-hunk read
    of the parents can certify it: tracing answers each hunk ALONE, and hunks
    that each match one parent still combine into bytes no generator produces.
    Those files keep every hunk, and the reviewer is asked for a whole-file
    check instead.

    Read in three trees and unioned: this checkout, the `merge`, and `head`. A
    rule the PR declares is absent from the base checkout, and one the
    resolution declares and a later commit drops is absent from the head. A
    tree the PR controls only ever ADDS a file here, so it raises scrutiny and
    never lowers it.
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
        "instructions name; do not give it a line-by-line verdict."
    )


def _safe_path(path: str) -> str:
    """A path fit for a note OUTSIDE a diff fence: whitespace collapsed and
    backticks stripped so it can't break the line or its span."""
    return re.sub(r"\s", " ", path).replace("`", "'")


def _whole_file_annotations(
    paths: list[str],
    superseded: dict[str, str],
    generated: frozenset[str],
    verified: dict[str, str] | None = None,
) -> list[str]:
    """The report lines for every path annotated away in whole — one the head
    has replaced with trusted bytes, and one a generator owns. Skipping a
    generated file's review is safe only because a required check re-derives
    its committed bytes from source on this head, which is what the rule's
    `rederivedByCheck` asserts. That flag is opt-in for both rule kinds, so a
    path no check re-derives reaches this report instead.
    """
    out = []
    verified = verified or {}
    for path in paths:
        safe = _safe_path(path)
        if path in verified:
            out += [
                f"**Regenerated (verified):** `{safe}` — {verified[path]}, so no "
                "hand wrote this delta and there is no provenance to read. Review "
                "its SOURCE instead.",
                "",
            ]
        elif path in superseded:
            out += [
                f"**Superseded at head:** `{safe}` — the PR head carries "
                f"{superseded[path]} for this file; nothing of this resolution's "
                "delta to it ships.",
                "",
            ]
        elif path in generated:
            out += [
                f"**Generator-owned:** `{safe}` — a build output "
                "(this repository's derived-file resolver owns its derivation). A "
                "required check re-derives its bytes from source on this head and "
                "compares them, which is what this rule's `rederivedByCheck` asserts "
                "— so a line-by-line provenance read of them says nothing; review "
                "its SOURCE instead.",
                "",
            ]
    return out


class Reviewable(NamedTuple):
    """What a merge still asks a reviewer to judge."""

    notes: list[str]
    """git's conflict notices, then the retired-hunk annotations, in path order."""
    diff: str
    """The remerge-diff of everything the reviewable paths still ship."""
    paths: list[str]
    """The paths that diff covers, in render order."""
    notices: list[str]
    """The conflict-notice block alone, also at the head of `notes`.

    Carried apart because it is the one part of `notes` that is NOT about a
    hunk: `_notice_lines` classifies a section as a notice precisely when it has
    no diff content, so a notice never reaches `diff`. A caller suppressing an
    empty section reads this to tell "every hunk retired, nothing to judge" from
    "git could not merge here at all".
    """


def _scope(kept: str, dropped: int, total: int, safe: str) -> str:
    """How much of a file's delta an annotation speaks for. "Every hunk" needs
    BOTH that none survives — a mode header outlives every hunk it carried, so a
    non-empty `kept` does not settle it — and that this pass accounts for the
    file's whole hunk count: when an earlier pass already retired some, the pass
    that empties the file speaks for its own share, not for the delta.
    """
    if dropped == total and "\n@@" not in f"\n{kept}":
        return f"every hunk of this resolution's delta to `{safe}`"
    return f"{dropped} of this resolution's hunks in `{safe}`"


def _corrected_note(kept: str, head_text: str, safe: str) -> list[str]:
    """The note pointing at every added line of `kept` the head does not carry.

    Positions and the path only, never a line's text — see
    `corrected_positions` for why quoting one would be a gate-steering channel.
    """
    located = [
        f"hunk {ordinal}, added line(s) {', '.join(map(str, positions))}"
        for ordinal, hunk in enumerate(_hunks(kept)[1], 1)
        if (positions := corrected_positions(hunk, head_text))
    ]
    if not located:
        return []
    return [
        f"**Corrected at head:** in `{safe}`, these added lines are absent from "
        "the PR head, so they do NOT ship — counting the `+` lines of each hunk "
        f"below in order: {'; '.join(located)}. They stay in the fence because "
        "the rest of each hunk ships. Raise no finding on them.",
        "",
    ]


# A lockfile key is PR-controlled text, unlike a Python identifier `ast` produces.
# Only this shape reaches a note outside the fence; anything else is counted, not
# quoted, so a crafted name cannot close its span and forge an annotation. `/`
# and `@` are in it because every npm key carries them, and neither closes a
# span or breaks a line — a backtick and a newline do, and both stay out.
_SAFE_ENTRY = re.compile(r"[A-Za-z0-9._@/-]{1,128}\Z")
# Ten names is a reviewer's whole read of one file. Past that the count carries
# the signal and the list stops being a place to look.
_SHARED_ENTRY_MAX = 10


def _collision_note(merged_text: str, blobs: ParentBlobs, safe: str) -> list[str]:
    """The note naming every top-level definition both parents added that this
    merge could only keep once.

    NAMES, not positions: `forced_collisions` carries why a per-line note would
    retire the wrong removal.
    """
    names = forced_collisions(merged_text, blobs)
    if not names:
        return []
    listed = ", ".join(f"`{name}`" for name in names)
    return [
        f"**Deduplicated by the merge:** in `{safe}`, both parents ADDED a "
        f"top-level definition named {listed}, and the merged file binds each "
        "one once, with one parent's own bytes. Python keeps only the last "
        "binding, so a file holding both copies would collect one and silently "
        "drop the other — the union resolution HAD to delete one. A removal "
        "inside such a definition is forced, not unexplained. This retires "
        "nothing: judge WHICH copy survived, and judge every other removal "
        "normally.",
        "",
    ]


def _shared_lock_entry_note(
    path: str, merged_text: str, head_text: str, blobs: ParentBlobs, safe: str
) -> list[str]:
    """The note naming every package both parents described identically that this
    merge describes differently, and the PR head has not since put back.

    A package name is the lockfile's own key, so a position in a file of
    thousands of lines points a reviewer at nothing. It is also PR-controlled,
    so a name outside `_SAFE_ENTRY` is counted rather than quoted.
    """
    changed = changed_shared_entries(merged_text, blobs.parent1, blobs.parent2, path)
    still = set(changed_shared_entries(head_text, blobs.parent1, blobs.parent2, path))
    changed = [name for name in changed if name in still]
    if not changed:
        return []
    safe_names = [name for name in changed if _SAFE_ENTRY.match(name)]
    unquotable = len(changed) - len(safe_names)
    shown = ", ".join(f"`{name}`" for name in safe_names[:_SHARED_ENTRY_MAX])
    rest = len(safe_names) - _SHARED_ENTRY_MAX
    tail = f", and {rest} more" if rest > 0 else ""
    if unquotable:
        tail += f", and {unquotable} whose name this cannot quote safely"
    return [
        f"**Both parents agreed:** in `{safe}`, this merge changes "
        f"{len(changed)} package entr{'y' if len(changed) == 1 else 'ies'} the "
        "two parents held IDENTICALLY, and the PR head still carries the "
        f"change: {shown or 'none this can name'}{tail}. No conflict existed on "
        "them, so no resolution choice was made — the lock tool moved them on "
        "its own. Read these first, and ask whether a manifest change one "
        "parent made asks for each one.",
        "",
    ]


def _relocated_note(
    kept: str, merge_text: str, mechanical_text: str, head_text: str, safe: str
) -> list[str]:
    """The note pointing at every removed line of `kept` that this merge kept and
    the head still ships — a line the resolution moved, not one it deleted.

    Positions and the path only, never a line's text: see `corrected_positions`
    for why quoting one would be a gate-steering channel.
    """
    located = [
        f"hunk {ordinal}, removed line(s) {', '.join(map(str, positions))}"
        for ordinal, hunk in enumerate(_hunks(kept)[1], 1)
        if (
            positions := relocated_positions(
                hunk, merge_text, mechanical_text, head_text
            )
        )
    ]
    if not located:
        return []
    return [
        f"**Still in the merged file:** in `{safe}`, these removed lines occur in "
        "this merge's own version of the file at least as often as in the mechanical "
        "merge, and the PR head still carries them — so the resolution MOVED them "
        "rather than deleting them. Counting the `-` lines of each hunk below in "
        f"order: {'; '.join(located)}. Raise no deletion finding on them, but DO "
        "judge where they moved TO: this counts occurrences and says nothing about "
        "position, so a guard lifted out of the branch it guarded reads as moved "
        "while the boundary it enforced is gone.",
        "",
    ]


# A retired hunk leaves the hunks beside it incomplete, and a MOVE is where that
# misleads: the resolution relocates a definition, the `-` half stays in the fence
# and the `+` half is retired, so the file reads as though the definition is gone.
# That produced a blocking finding on a merge whose merged file defines the symbol.
_RETIRED_HUNK_CAVEAT = (
    " Those hunks are NOT in the fence below, so a hunk that survives can read as "
    "incomplete: a definition it removes may be re-added by one of them. The "
    "**Still in the merged file:** note names the removed lines this merge's own "
    "file keeps, so read it — its ABSENCE means no removed line survives — before "
    "raising a finding that something was deleted."
)


class MergeRefs(NamedTuple):
    """The revisions one merge's per-file reads resolve against."""

    merge: str
    head: str
    base: str
    """The parents' merge-base."""
    parent1: str
    parent2: str
    mechanical: str
    """The mechanical 3-way merge of the parents, as a tree oid."""


def _path_annotations(
    refs: MergeRefs, path: str, file_diff: str, trace: bool = True
) -> tuple[list[str], str]:
    """The trusted notes for one path's delta, and the part of it that still ships.

    Two passes retire a hunk without a verdict: the head has undone it, or the
    parents' own edits against their merge-base account for every line it touches.

    `trace=False` drops the SECOND pass only, for a path whose content the merged
    tree fixes. Tracing answers each hunk alone, so hunks that each match one
    parent still combine into bytes no generator produces. The undone-at-head
    pass and the two anti-false-positive notes still run: a derived path is the
    worst case for `_RETIRED_HUNK_CAVEAT`, since a line reappearing verbatim
    elsewhere in a lockfile is ordinary.
    """
    safe = _safe_path(path)
    total = file_diff.count("\n@@ ")
    merged_text = _blob(refs.merge, path)
    head_text = _blob(refs.head, path)
    notes: list[str] = []
    kept, undone = _drop_hunks(
        file_diff,
        lambda h: hunk_undone_at_head(h, head_text, merged_text),
    )
    if undone:
        notes += [
            f"**Undone at head:** {_scope(kept, undone, total, safe)} — gone from "
            "the PR head, added lines absent and removed lines back, so that "
            "much of the delta does not ship.",
            "",
        ]
    blobs = ParentBlobs(
        _blob(refs.base, path), _blob(refs.parent1, path), _blob(refs.parent2, path)
    )
    traced = 0
    if trace:
        kept, traced = _drop_hunks(kept, lambda h: hunk_traced_to_the_parents(h, blobs))
    if traced:
        notes += [
            f"**Traced to the parents:** {_scope(kept, traced, total, safe)} — trusted "
            f"code compared this file at the parents' merge-base `{refs.base[:12]}` and "
            "at both parents: every line those hunks remove was deleted by a "
            "parent since that base, and every line they add was added by one. "
            "Nothing in them is content neither parent has."
            + (_RETIRED_HUNK_CAVEAT if "\n@@" in f"\n{kept}" else ""),
            "",
        ]
    if kept:
        notes += _relocated_note(
            kept, merged_text, _blob(refs.mechanical, path), head_text, safe
        )
        # Python only: `forced_collisions` parses all four texts, so any other
        # language answers with no note and its removals stay under review.
        if path.endswith(".py"):
            notes += _collision_note(merged_text, blobs, safe)
        if lockfile_rule_for(path) is not None:
            notes += _shared_lock_entry_note(path, merged_text, head_text, blobs, safe)
        notes += _corrected_note(kept, head_text, safe)
    return notes, kept


def _hunk_annotations_and_diff(
    sha: str,
    head: str,
    paths: list[str],
    annotated: list[str],
    parents: list[str],
    derived: frozenset[str] = frozenset(),
) -> Reviewable:
    """The trusted per-hunk notes for the still-reviewable paths, the diff of
    everything those paths still ship, and those paths themselves.

    The path list is returned rather than recovered from the rendered diff,
    since a header is ambiguous for a path containing " b/".

    A DERIVED path takes no TRACING. Tracing answers each hunk alone, so hunks
    that each match one parent still combine into bytes no generator produces —
    the reviewer needs the whole file. Its other notes still run, and the two
    anti-false-positive ones matter most there: a lockfile line that reappears
    verbatim elsewhere is ordinary, and suppressing `_relocated_note` is how a
    moved definition reads as a deletion.
    """
    deltas, notices = _reviewable_diffs(sha, paths, annotated)
    notes = _conflict_notice_note(notices)
    if not deltas:
        return Reviewable(notes, "", [], notices)
    # After the emptiness check: `merge-base` EXITS NON-ZERO on parents with no
    # common ancestor, and a fully-annotated merge has no use for it.
    refs = MergeRefs(
        merge=sha,
        head=head,
        base=_git("merge-base", parents[1], parents[2]).strip(),
        parent1=parents[1],
        parent2=parents[2],
        mechanical=_mechanical_tree(parents[1], parents[2]),
    )
    shown, shown_paths = [], []
    for path, file_diff in deltas:
        path_notes, kept = _path_annotations(
            refs, path, file_diff, trace=path not in derived
        )
        notes += path_notes
        if kept:
            shown.append(kept)
            shown_paths.append(path)
    return Reviewable(notes, "".join(shown), shown_paths, notices)


def _section(sha: str, head: str | None) -> str:
    """The report section for one merge commit; empty when it matches the
    mechanical merge."""
    parents = _git("rev-list", "--parents", "-n1", sha).split()
    if len(parents) < 3:  # the commit itself + fewer than two parents
        raise SystemExit(
            f"{sha} is not a merge commit, so it has no resolution to review. "
            "--remerge-diff would print its ordinary diff, and reporting that as "
            "a hand-authored merge delta would accuse a normal commit."
        )
    if len(parents) > 3:  # the commit itself + more than two parents
        raise SystemExit(
            f"merge {sha} has {len(parents) - 1} parents; --remerge-diff cannot "
            "reconstruct an octopus merge, so its resolution cannot be reviewed "
            "this way. Re-merge as a chain of two-parent merges."
        )
    paths = _delta_paths(sha)
    if not paths:
        return ""
    # `head is None` is the single-commit caller: comparing the merge against
    # its own tree would mark every ordinary resolution superseded.
    derived = _merged_tree_derived(paths, sha, head or sha)
    # A derived file's one correct content is what the MERGED tree fixes, so
    # bytes equal to a parent's are staleness, not evidence — one side's file
    # beside the other side's number. No retirement may speak for it.
    superseded = (
        {}
        if head is None
        else {
            path: why
            for path, why in _superseded_paths(parents, head, paths).items()
            if path not in derived
        }
    )
    # The two GENERATOR annotations do apply to a derived path: `-merge` only
    # says no PARENT'S bytes can vouch for it, and a check or a fresh generator
    # run judges the merged tree itself — the whole-file answer `_derived_note`
    # asks for. Kept whole, a lockfile and three bundles starved #4921's review.
    generated = frozenset(paths) & _generated_paths()
    regen = _verified_regenerated(sha, paths)
    derived = derived - generated - frozenset(regen.verified)
    subject = _git("log", "-1", "--format=%s", sha).strip().replace("`", "'")
    # Collapsed by default so several merges don't dominate the PR page. A
    # blank line after <summary> is required for GitHub to render the fence.
    annotated = [
        p for p in paths if p in superseded or p in generated or p in regen.verified
    ]
    parts = _whole_file_annotations(paths, superseded, generated, regen.verified)
    derived_note = _derived_note(paths, derived)
    if derived_note:
        parts += [derived_note, ""]
    for path in regen.mismatched:
        # Not for a derived path: its note above already asks for the whole-file
        # read, and the hunk-read this one asks for is what `-merge` makes
        # unsound — together they are two contradictory instructions.
        if path in derived:
            continue
        parts += [
            f"**Regenerated output does NOT match:** `{_safe_path(path)}` — a "
            "fresh run of this repository's own generator produces different "
            "bytes, so every hunk below is hand-authored. Read them.",
            "",
        ]
    notes, diff, shown_paths, notices = _hunk_annotations_and_diff(
        sha, head or sha, paths, annotated, parents, derived
    )
    parts += notes
    # A merge every filter retired renders NOTHING, rather than a section saying so: the pull request comment would carry a row per clean merge, and self_review.py reads a non-empty report as "there is something to review" and spends a model run on it. The hunk annotations are vacuous with no hunk below.
    # A conflict NOTICE is not one of them, which is why `notices` is carried apart: it names a path git could not merge at all, carries no hunk by construction, and is where a wrong resolution is most likely.
    if not diff.strip() and not notices:
        return ""
    if diff.strip():
        lines = diff.strip().count("\n") + 1
        size = f"{lines}-line delta"
        fence = _fence(diff)
        parts.append(f"{fence}diff\n{diff.rstrip()}\n{fence}")
        # shown_paths is exactly the set the fence above renders.
        parts.append(_provenance(parents[1], parents[2], shown_paths))
        parts.append("")
    else:
        size = "no hunk to judge; git could not merge the paths named above"
    body = "\n".join(parts)
    return (
        f"\n<details><summary><code>{sha[:12]}</code> {subject} "
        f"({size})</summary>\n\n{body}</details>\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Render merge-resolution deltas.")
    parser.add_argument(
        "--commit",
        help="report only this merge commit, uncapped, instead of every merge in "
        "BASE_SHA..HEAD_SHA",
    )
    parser.add_argument(
        "--settled-merges",
        help="PATH holding merge shas, one per line, that somebody has already "
        "traced to their parents on this PR; omit their sections from the report "
        "and from --shas-out. Only the reviewer's own input sets this: it makes a "
        "settled merge unable to draw the same finding a second time, so the cost "
        "of settling a long-lived PR is the merges nobody has read, not every "
        "merge on the head. A missing or unreadable file omits nothing.",
    )
    parser.add_argument(
        "--shas-out",
        help="also write the sha of every merge this report renders a section "
        "for, one per line, to PATH (empty file when there is nothing to "
        "review); incompatible with REMERGE_REPORT_MAX_BYTES",
    )
    args = parser.parse_args()
    # The caller's wiring is read BEFORE the tree is bound. Binding first turns a
    # missing BASE_SHA into "not a git repository" from whatever directory the
    # process sits in, which names neither the variable nor the caller.
    base = None if args.commit else os.environ["BASE_SHA"]
    # The merge-attribute reads below go through `_git_io`, which refuses an
    # unbound call so a destructive command cannot reach whatever tree the
    # process happens to sit in. This report's tree is its own cwd.
    bind_repo(".")
    if args.commit:
        # No head: nothing after this merge could supersede its delta.
        merges, max_bytes, head = [args.commit], None, None
    else:
        head = os.environ["HEAD_SHA"]
        merges = list(reversed(_git("rev-list", "--merges", f"{base}..{head}").split()))
        # Opt-in, no default: only the PR-comment caller has a byte limit. The
        # two AUDIT callers have none, since shrinking silently would let a
        # merge go unreviewed.
        cap = os.environ.get("REMERGE_REPORT_MAX_BYTES")
        max_bytes = int(cap) if cap is not None else None
    if args.settled_merges:
        settled_path = Path(args.settled_merges)
        # A missing settled-set file means NO filtering, never a hidden merge.
        settled = (
            set(settled_path.read_text(encoding="utf-8").split())
            if settled_path.exists()
            else set()
        )
        merges = [sha for sha in merges if sha not in settled]
    sections = [(sha, _section(sha, head)) for sha in merges]
    sections = [(sha, text) for sha, text in sections if text]
    # Written before the empty-report return, so the file always reflects THIS run.
    if args.shas_out:
        # The cap DROPS sections below, so a "already judged" consumer would
        # retire a merge whose delta never reached the reader; mutually exclusive.
        if max_bytes is not None:
            raise SystemExit(
                "--shas-out cannot be combined with REMERGE_REPORT_MAX_BYTES: a "
                "dropped section would be recorded as a merge the report covered."
            )
        Path(args.shas_out).write_text(
            "".join(f"{sha}\n" for sha, _ in sections), encoding="utf-8"
        )
    if not sections:
        return
    # Two rules, both about WHAT gets dropped under the byte cap:
    #   - truncate at section boundaries, never mid-fence, or an open fence would
    #     render the notice as diff content. No cap means no dropping at all;
    #   - choose the dropped section NEWEST first, keeping DISPLAY order
    #     oldest-first. Sections render oldest-first, so filling the budget in
    #     display order drops the newest merge — the one just pushed, the one
    #     nobody has read, and the only one whose verdict is still owed.
    kept, dropped, size = set(), [], len(_INTRO.encode())
    for index in reversed(range(len(sections))):
        sha, text = sections[index]
        if max_bytes is not None and size + len(text.encode()) > max_bytes:
            dropped.append(sha[:12])
        else:
            kept.add(index)
            size += len(text.encode())
    dropped.reverse()
    report = _INTRO + "".join(
        text for index, (_sha, text) in enumerate(sections) if index in kept
    )
    if dropped:
        report += (
            f"\n**…{len(dropped)} merge(s) {OMITTED_NOTICE} "
            f"({', '.join(f'`{sha}`' for sha in dropped)}). "
            "The merge-delta reviewer's copy is uncapped and carries them; run "
            "`git show --remerge-diff <sha>` to read them here.**\n"
        )
    print(report)


if __name__ == "__main__":
    main()
