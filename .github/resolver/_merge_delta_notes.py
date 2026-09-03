"""Every trusted note the merge-resolution delta report writes for the reviewer, and the two helpers that make a string safe to write.

PROBLEM CLASS — what may a report say ABOUT a pull request's own bytes, outside the fence the reviewer is told to trust? A note here is read as trusted input, so it carries counts, positions and identifiers a parser produced, and never a line of PR-authored text. `fence` and `safe_path` are the two places a PR-controlled string is admitted at all, and each strips what could close its span.

Pure text: every function takes the diff, the reference blobs and the counts it needs, and reads no repository. `remerge-diff-report.py` owns the git side and calls these.
"""

import re

from _merge_delta_novelty import (  # noqa: I001
    ParentBlobs,
    blocks_carried_at_head,
    corrected_positions,
    forced_collisions,
    relocated_positions,
)
from _shared_lock_entries import changed_shared_entries


def fence(text: str) -> str:
    """A backtick fence longer than any run inside `text`, so PR-controlled diff content
    cannot break out of its data block."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def safe_path(path: str) -> str:
    """A path fit for a note OUTSIDE a diff fence: whitespace collapsed and
    backticks stripped so it can't break the line or its span."""
    return re.sub(r"\s", " ", path).replace("`", "'")


def conflict_notice_note(notices: list[str]) -> list[str]:
    """The report lines for the paths the mechanical merge could not resolve —
    reported, never dropped, since git could not merge there. Fenced since the
    branch names and commit subjects inside are PR-author text.
    """
    if not notices:
        return []
    text = "\n".join(notices)
    edge = fence(text)
    return [
        "**Paths the mechanical merge could not resolve** — git reports these "
        "conflicts with no content delta of their own, so this report has no hunk "
        "to judge for them. Each line names the path and the kind of conflict:",
        "",
        f"{edge}\n{text}\n{edge}",
        "",
    ]


def derived_note(paths: list[str], derived: frozenset[str]) -> str:
    """The line naming every kept path whose content only the merged tree fixes."""
    named = sorted(set(paths) & derived)
    if not named:
        return ""
    listed = ", ".join(f"`{safe_path(p)}`" for p in named)
    return (
        f"**Derived from the merged tree:** {listed} — git is told never to "
        "line-merge these (`-merge`), so no hunk of them is retired as traced to "
        "a parent. Judge each as a whole file, and ask for the check the "
        "instructions name; do not give it a line-by-line verdict."
    )


def whole_file_annotations(
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
        safe = safe_path(path)
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


def scope(kept: str, dropped: int, total: int, safe: str) -> str:
    """How much of a file's delta an annotation speaks for. "Every hunk" needs
    BOTH that none survives — a mode header outlives every hunk it carried, so a
    non-empty `kept` does not settle it — and that this pass accounts for the
    file's whole hunk count: when an earlier pass already retired some, the pass
    that empties the file speaks for its own share, not for the delta.
    """
    if dropped == total and "\n@@" not in f"\n{kept}":
        return f"every hunk of this resolution's delta to `{safe}`"
    return f"{dropped} of this resolution's hunks in `{safe}`"


def corrected_note(hunks: list[str], head_text: str, safe: str) -> list[str]:
    """The note pointing at every added line of `hunks` the head does not carry.

    Positions and the path only, never a line's text — see
    `corrected_positions` for why quoting one would be a gate-steering channel.
    """
    located = [
        f"hunk {ordinal}, added line(s) {', '.join(map(str, positions))}"
        for ordinal, hunk in enumerate(hunks, 1)
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


def collision_note(merged_text: str, blobs: ParentBlobs, safe: str) -> list[str]:
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


def shared_lock_entry_note(
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


def relocated_note(
    hunks: list[str],
    merge_text: str,
    mechanical_text: str,
    head_text: str,
    safe: str,
) -> list[str]:
    """The note pointing at every removed line of `hunks` that this merge kept and
    the head still ships — a line the resolution moved, not one it deleted.

    Positions and the path only, never a line's text: see `corrected_positions`
    for why quoting one would be a gate-steering channel.
    """
    located = [
        f"hunk {ordinal}, removed line(s) {', '.join(map(str, positions))}"
        for ordinal, hunk in enumerate(hunks, 1)
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
RETIRED_HUNK_CAVEAT = (
    " Those hunks are NOT in the fence below, so a hunk that survives can read as "
    "incomplete: a definition it removes may be re-added by one of them. The "
    "**Still in the merged file:** note names the removed lines this merge's own "
    "file keeps, so read it — its ABSENCE means no removed line survives — before "
    "raising a finding that something was deleted."
)


CARRIAGE_RETIRED = (
    "The retiring annotation above says this resolution's delta does not ship; "
    "it never says the head lacks that content."
)

CARRIAGE_DERIVED = (
    "The derived-file note above asks you for a whole-file concern; a concern "
    "about content these counts say the head does not carry names a state this "
    "PR does not ship."
)


def head_carriage_note(
    hunks: list[str], head_text: str, safe: str, because: str
) -> list[str]:
    """What the PR head still holds of one file's delta, as two counts. BECAUSE
    says which read of that file the counts correct.

    Two callers share one gap — a file whose delta the reviewer must judge with
    no head read of its own. A RETIRED file's annotation says the delta does not
    SHIP, which never says the head lacks that content: supersession carries a
    PARENT's bytes, and that parent can already hold every line the resolution
    added. A file only the MERGED tree fixes is the other, and there the prompt
    demands a whole-file concern, so the reviewer raises one about content a
    later commit has already rewritten.

    Counts, never the lines themselves: this note sits OUTSIDE the diff fence,
    the region the prompt tells the reviewer to trust, and `corrected_positions`
    carries why quoting PR-controlled text there is a gate-steering channel.
    """

    def carried(sign: str) -> tuple[int, int]:
        """This side's totals across every hunk of the file, summed from the
        per-hunk counts `blocks_carried_at_head` owns."""
        per_hunk = [blocks_carried_at_head(h, sign, head_text) for h in hunks]
        return sum(n for n, _ in per_hunk), sum(total for _, total in per_hunk)

    added_at_head, added = carried("+")
    removed_at_head, removed = carried("-")
    if not added and not removed:
        return []
    return [
        f"**Head carriage:** `{safe}` — of the {added} block(s) this resolution "
        f"added here the PR head carries {added_at_head}, and of the {removed} it "
        f"removed the head still carries {removed_at_head}. A block is a run of "
        f"consecutive lines, counted whole. {because} Raise no finding about what "
        "this file holds at head that these counts contradict.",
        "",
    ]
