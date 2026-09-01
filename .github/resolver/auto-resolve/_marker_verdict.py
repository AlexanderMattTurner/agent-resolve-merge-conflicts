"""The leftover-marker verdict: WHY markers remain, and what a human gets.

PROBLEM CLASS — the same leftover conflict markers have opposite causes: a
model that judged the merge and declined it, a shard whose edit tool was
DENIED, a shard the fan-out's WALL CLOCK killed or never started, and a shard
that ran and answered nothing. Each cause needs a different next step from a
human (finish the merge, fix the grants, give the fan-out room, fix the
resolver), so the refusal here names the cause it can prove and hands over the
salvage patch for whatever did resolve. The first three are provable from
records the run writes: the denied tool NAMES, the shard's own `declined`
record, and its `timed_out` flag. What is left over is the fourth.

bundle.py binds a :class:`MarkerVerdict` to one run's state via
``Bundle.marker_verdict()`` and refuses through it; the helpers below are the
shared readers its other marker checks (``salvage_declined_paths``) use.
"""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _denials import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Denials,
    denials_blocked_a_marker_file,
    edit_tool_was_denied,
)
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    git,
    git_lines,
    git_status,
)
from _result_fields import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    file_answered,
    shards_by_file,
    unanswered_files,
)
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    apply_blocked_label,
    escalation_block,
    fail,
)

_SHARED_NAMES = json.loads(
    (Path(__file__).resolve().parent.parent / "lib" / "shared-names.json").read_text(
        encoding="utf-8"
    )
)
_LABEL_AUTO_RESOLVE_BLOCKED = _SHARED_NAMES["pr_labels"]["auto_resolve_blocked"]

# A line marking an unresolved hunk. lib.sh reads it from the same file, so this
# step and the shell steps beside it cannot spell it differently.
CONFLICT_MARKER_RE = _SHARED_NAMES["auto_resolve"]["conflict_marker_re"]

# How many conflicted paths a refusal comment names before it counts the rest. A
# template sync conflicts in dozens of files, and the list is a sentence in a PR
# comment, not a report.
_MARKER_FILES_NAMED = 10


def marker_file_text(paths: list[str]) -> str:
    """The conflicted paths, as the text a refusal comment names them in."""
    named = ", ".join(f"`{path}`" for path in paths[:_MARKER_FILES_NAMED])
    remaining = len(paths) - _MARKER_FILES_NAMED
    return f"{named}, and {remaining} more" if remaining > 0 else named


def _fanout_dir() -> str:
    """Where the fan-out left its records, the default matching fanout.py's."""
    return (
        os.environ.get("FANOUT_DIR")
        or f"{os.environ.get('RUNNER_TEMP', '/tmp')}/conflict-fanout"  # noqa: S108
    )


def declined_widened_paths(repo: Path) -> set[str]:
    """The repo-relative paths only DECLINED shards edited under their widened
    grant, read from the hook's per-shard logs (`<index>.widened`). A path a
    resolving shard also edited stays out: that edit is part of a resolution
    this run lands. An unreadable log reads as no edits, the safe direction —
    the path then lands and is named on the pull request as a widened edit."""
    edited: dict[bool, set[str]] = {True: set(), False: set()}
    for shard in _execution_shards():
        if "index" not in shard:
            continue
        try:
            lines = Path(_fanout_dir(), f"{shard['index']}.widened").read_text(
                encoding="utf-8"
            )
        except OSError:
            continue
        for line in lines.splitlines():
            try:
                relative = Path(line).resolve().relative_to(repo.resolve())
            except ValueError:
                continue
            edited[bool(shard.get("declined"))].add(str(relative))
    return edited[True] - edited[False]


def _execution_shards() -> list[dict]:
    """This run's per-shard records, or none when the log cannot be read.

    An unreadable log answers the empty list: the readers below only sharpen a
    diagnosis, so one must never be the reason a refusal cannot be published."""
    try:
        document = json.loads(
            Path(_fanout_dir(), "execution.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    shards = document.get("shards") if isinstance(document, dict) else None
    if not isinstance(shards, list):
        return []
    return [shard for shard in shards if isinstance(shard, dict) and shard.get("file")]


def files_starved_of_clock() -> set[str]:
    """The paths whose shards all ended on the fan-out's WALL CLOCK — killed at
    SHARD_TIMEOUT_SECONDS, or never started because FANOUT_BUDGET_SECONDS was
    already spent on the shards before them.

    No model read these hunks, so their markers are the ORIGINAL conflict. Every
    other reader here drops them: `unanswered_files` excludes a file with an
    errored shard, so without this set a truncated fan-out reaches the final
    verdict below and publishes the wall clock as the model's own judgement.

    A file counts as answered only under `file_answered`'s rule, which the residue
    pass cannot soften here: `_residue_files` gives no whole-file retry to a file
    holding an errored shard, so a block killed by the clock beside a resolved
    block is nobody's business but this one."""
    starved = set()
    for file, file_shards in shards_by_file(_execution_shards()).items():
        if file_answered(file_shards):
            continue
        if any(shard.get("timed_out") for shard in file_shards):
            starved.add(file)
    return starved


def files_with_no_deliverable() -> set[str]:
    """The paths whose shard RAN, reported success, and answered NOTHING — no
    marker-free file and no recorded decline.

    Three causes wear the same leftover markers, and each needs a different next
    step from a human. A model that DECLINED the merge is a conflict for a human
    to finish; a shard whose credential died is the ladder's problem; a shard
    that answered nothing is the resolver falling short. Reading a decline as the
    third sends a human to file a resolver bug against a judgement, and reading
    the third as a decline sends them to finish markers nobody judged. The
    decline record (`declined`) is what separates them, so this set is the
    residue after both other causes are taken out."""
    # PER FILE, through the one definition the fan-out also calls: a block shard
    # that answered nothing does not make the file unanswered when the residue
    # retry's whole-file shard went on to resolve or decline it. Judging each
    # shard alone reported those files as faults and refused to salvage them.
    return unanswered_files(_execution_shards())


# One sentence per path is what the refusal comment quotes, so a reasoning longer
# than this is a report the comment was never meant to carry.
_REASON_CHARS = 1024


def declined_files() -> dict[str, str]:
    """The paths a shard recorded a DECLINE for, each with the reasoning it gave.

    A path with several declining shards keeps the first reasoning that is not
    empty, because the refusal comment quotes one sentence per path.

    Each reasoning is TRUNCATED here, at the one place every consumer reads it.
    A shard writes it after reading the conflicted file, so the PR branch's own
    content influences it, and this is the only path carrying free-form model
    text into the sticky comment. An unbounded one could also push the comment
    past what `gh` will post, which would cost the refusal itself."""
    reasons: dict[str, str] = {}
    for shard in _execution_shards():
        if not shard.get("declined"):
            continue
        reason = (shard.get("decline_reason") or "")[:_REASON_CHARS]
        if not reasons.get(shard["file"]):
            reasons[shard["file"]] = reason
    return reasons


def _decline_reasons(marker_files: list[str]) -> str:
    """What the model SAID about the paths it declined, as one sentence appended
    to the refusal comment, or empty when it recorded no reasoning.

    The reasoning is the whole value of a decline to the human who now owns the
    merge: without it the comment says a conflict is too hard and nothing about
    which part or why."""
    reasons = declined_files()
    quoted = [
        f"`{path}`: {reasons[path].strip()}"
        for path in marker_files
        if reasons.get(path, "").strip()
    ]
    if not quoted:
        return ""
    return " The resolver's own account of what it would not merge — " + "; ".join(
        quoted
    )


# How many times one head may carry a partial resolution forward. The progress
# test below is what ends an ordinary chain, so this is the backstop for a chain
# that keeps shrinking the set and never reaches zero. A round reaches roughly
# `MAX_PARALLEL x (FANOUT_BUDGET_SECONDS / SHARD_TIMEOUT_SECONDS)` files, so a
# cap near the reachable width of one round would refuse the sets this carry
# exists for.
_MAX_CARRY_ROUNDS = 10


def _carried() -> dict:
    """The salvage manifest THIS run was given, empty when it carried none."""
    manifest = os.environ.get("SALVAGE_DIR", "")
    if not manifest:
        return {}
    try:
        document = json.loads(
            Path(manifest, "salvage.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def carried_round() -> int:
    """Which carry round this run is. Zero when it carried nothing, so a first
    refusal writes round 1."""
    recorded = _carried().get("round")
    return recorded if isinstance(recorded, int) and recorded > 0 else 0


def continue_partial(resolved: list[str]) -> bool:
    """Whether this refusal has earned another paid run on the same head.

    Three bounds, and each one stops a conflict nobody can finish from spending
    forever. The wall clock must be why paths are still marked, because that is
    the only cause another window removes — a decline and a denied grant both
    reproduce exactly. The chain is capped. And a round that resolved no more
    paths than the one before it made no progress a further round builds on."""
    if not resolved or not files_starved_of_clock():
        return False
    if carried_round() + 1 >= _MAX_CARRY_ROUNDS:
        return False
    carried = _carried().get("paths")
    return len(resolved) > (len(carried) if isinstance(carried, list) else 0)


def _publish_carry(resolved: list[str]) -> None:
    """Tell the workflow whether this refusal earned another run on this head.

    The step exits non-zero right after, so this is the only channel the
    continuation gate can read: an output written before the failure survives
    it, while a return value does not."""
    output = os.environ.get("GITHUB_OUTPUT", "")
    if not output:
        return
    with open(output, "a", encoding="utf-8") as handle:
        handle.write(f"carry_round={carried_round() + 1}\n")
        handle.write(f"carry_continue={'true' if continue_partial(resolved) else ''}\n")


@dataclass(frozen=True)
class MarkerVerdict:
    """One run's leftover-marker refusal, bound to the state that decides it:
    the conflicted set, what the execution log said about denials, and the two
    parents the salvage patch diffs against."""

    allowed: list[str]
    denials: Denials
    pr: str
    bundle_dir: Path
    checked_out_head: str
    merge_base_side: str

    def refuse_leftover_markers(self, *pathspec: str) -> None:
        """Abort if any tracked file matching PATHSPEC still carries conflict
        markers, with a verdict that says WHY they are there."""
        if git_status("grep", "-nE", CONFLICT_MARKER_RE, "--", *pathspec) != 0:
            return
        print("Conflict markers still present:")
        print(
            git("grep", "-nE", CONFLICT_MARKER_RE, "--", *pathspec, check=False), end=""
        )
        # Only a denial on one of THESE can be why the resolution is incomplete.
        marker_files = git_lines("grep", "-lE", CONFLICT_MARKER_RE, "--", *pathspec)
        self._diagnose_markers(marker_files)

    def still_marked(self) -> set[str]:
        """Every tracked path carrying a conflict marker right now, over the
        whole tree — independent of whichever PATHSPEC the check that is about
        to refuse used, so a deferred path not yet regenerated is never
        miscounted as resolved."""
        if git_status("grep", "-qE", CONFLICT_MARKER_RE, "--", ".") != 0:
            return set()
        return set(git_lines("grep", "-lE", CONFLICT_MARKER_RE, "--", "."))

    def write_salvage_patch(self) -> tuple[list[str], bool]:
        """Diff the conflicted paths that carry no marker right now against the
        merge base, and write it into BUNDLE_DIR — the directory the workflow
        already uploads as this run's `auto-resolve-merge-<pr>` artifact on
        success — so a leftover-markers refusal still hands `land` the paths
        that resolved instead of discarding them with the rest.

        The patch ships beside `salvage.json`, which is what makes it
        INSTALLABLE rather than only readable: the next run for this head
        restores each path to the recorded merge base, applies this patch there,
        and stages the result, so its own window buys only what is still
        conflicted. Both pins are load-bearing — a patch cut from another merge
        base applies to text neither side wrote.

        Returns the resolved paths and whether a non-empty patch was written;
        the caller names both in its refusal comment."""
        resolved = sorted(set(self.allowed) - self.still_marked())
        if not resolved:
            return resolved, False
        merge_base = git(
            "merge-base", self.checked_out_head, self.merge_base_side
        ).strip()
        patch = git("diff", merge_base, "--", *resolved)
        if not patch:
            return resolved, False
        self.bundle_dir.mkdir(parents=True, exist_ok=True)
        (self.bundle_dir / "salvage.patch").write_text(patch, encoding="utf-8")
        (self.bundle_dir / "salvage.json").write_text(
            json.dumps(
                {
                    "head": os.environ.get("HEAD_SHA", ""),
                    "merge_base": merge_base,
                    "paths": resolved,
                    "round": carried_round() + 1,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return resolved, True

    def salvage_note(self) -> str:
        """Write the salvage patch and say where it went, as the one sentence
        every refusal that discards a paid resolution appends to its comment.

        Empty when nothing resolved, so a caller appends it unconditionally."""
        resolved, salvaged = self.write_salvage_patch()
        _publish_carry(resolved if salvaged else [])
        if not salvaged:
            return ""
        return (
            f" {len(resolved)} of {len(self.allowed)} conflicted file(s) "
            "resolved cleanly before this refusal; the patch for what "
            f"succeeded is attached to this run's `auto-resolve-merge-{self.pr}` "
            "artifact."
        )

    def _diagnose_markers(self, marker_files: list[str]) -> NoReturn:
        """Distinguish a deliberate handoff from a resolution the LLM was DENIED
        permission to write — the same leftover markers, opposite causes.

        Only the denied tool NAMES decide it, so all three states get their own
        diagnosis."""

        def refuse(
            error: str,
            comment: str,
            *,
            resolver_fault: bool = False,
            declined: bool = False,
            escalate: str = "",
        ) -> NoReturn:
            """Every verdict names the files a human must finish. The comment IS the
            handoff, so one that withholds the list sends its reader to the run log
            before they can start.

            ``resolver_fault`` rides through to :func:`fail`: two of the branches
            below cannot say the model declined anything, because a denied edit tool
            may have closed the write path — and there the fix is a grant, not a push
            to this branch. ``declined`` is the opposite end: only the last branch
            has ruled every harness cause out, so only it can call these markers the
            model's own verdict."""
            fail(
                error,
                f"{comment} Still conflicted: {marker_file_text(marker_files)}."
                f"{self.salvage_note()}",
                resolver_fault=resolver_fault,
                declined=declined,
                escalate=escalate,
            )

        if self.denials.count > 0:
            if self.denials.tools is None:
                # Neither cause is established: name that, rather than picking one.
                refuse(
                    f"conflict markers still present after {self.denials.count} "
                    "permission denial(s) whose tools the execution log did not name",
                    f"the resolver hit {self.denials.count} permission denial(s) and "
                    "the execution log did not name the tools, so this run "
                    "cannot say whether its edits were blocked or whether it "
                    "judged the conflict unmergeable and left the markers "
                    "deliberately. Check the resolver's tool grants before "
                    "reading these markers as a hard conflict.",
                    resolver_fault=True,
                )
            if (
                self.denials.by_file is not None
                and edit_tool_was_denied(self.denials.tools)
                and not denials_blocked_a_marker_file(
                    self.denials.by_file, marker_files
                )
            ):
                # A denial on another file's shard cost this resolution nothing, so
                # it must not label the whole PR out of auto-resolve.
                refuse(
                    f"conflict markers still present in the tree; the "
                    f"{self.denials.count} permission denial(s) "
                    f"({self.denials.text}) landed on other files' shards",
                    "the resolution left conflict markers behind. (The resolver "
                    f"was denied {self.denials.count} permission(s) — "
                    f"`{self.denials.text}` — but none of them on a shard whose "
                    "file still carries markers, so they did not block this "
                    "resolution. Auto-resolve stays enabled on this PR.)",
                )
            if edit_tool_was_denied(self.denials.tools):
                # A closed write path repeats on every base push, so stop retrying.
                apply_blocked_label(
                    self.pr, _LABEL_AUTO_RESOLVE_BLOCKED, "Auto-resolve"
                )
                refuse(
                    f"conflict markers still present after {self.denials.count} "
                    f"permission denial(s), including an edit tool ({self.denials.text})",
                    f"the resolver was denied permission {self.denials.count} time(s) "
                    f"— including an edit tool (`{self.denials.text}`) — so it "
                    "could not apply its edits: a permission/config problem, not "
                    "a conflict too hard to merge. The markers are the ORIGINAL, "
                    "unresolved conflict. Auto-resolve is labelled "
                    f"`{_LABEL_AUTO_RESOLVE_BLOCKED}` and will skip this PR "
                    "until the grants are fixed and the label removed.",
                    resolver_fault=True,
                )
            refuse(
                "conflict markers still present in the tree; the "
                f"{self.denials.count} permission denial(s) were all non-edit tools "
                f"({self.denials.text}) and did not block the resolution",
                "the resolution left conflict markers behind. (The resolver was "
                f"also denied {self.denials.count} non-edit tool(s) — "
                f"`{self.denials.text}` — which cannot have blocked an edit, so "
                "they are not the cause.)",
            )
        if starved := sorted(files_starved_of_clock() & set(marker_files)):
            # Marked handed off but NOT declined: raising the fan-out's room is a
            # change to the RESOLVER, and discover retires a handoff mark when the
            # resolver's code moves. A decline mark would hold this head until a
            # human pushed to it, for hunks no model ever read.
            refuse(
                "conflict markers still present in the tree; the shard(s) for "
                f"{', '.join(starved)} ended on the fan-out's wall clock",
                "the fan-out ran out of wall clock before it resolved "
                f"{marker_file_text(starved)} — those shards were killed at "
                "`SHARD_TIMEOUT_SECONDS` or never started inside "
                "`FANOUT_BUDGET_SECONDS`, so no model read these hunks and "
                "nothing here is a judgement that the conflict is too hard. One "
                "fan-out reaches about (`FANOUT_BUDGET_SECONDS` / "
                "`SHARD_TIMEOUT_SECONDS`) x `MAX_PARALLEL` shards, so a conflict "
                "set past that size stops at the same place however often it "
                "runs.",
            )
        if undelivered := sorted(files_with_no_deliverable() & set(marker_files)):
            refuse(
                "conflict markers still present in the tree; the shard(s) for "
                f"{', '.join(undelivered)} ran, reported success, wrote no "
                "marker-free file and recorded no decline",
                "the resolver produced no resolution for "
                f"{marker_file_text(undelivered)} — its shard ran, reported "
                "success, wrote no marker-free file and recorded no decline, so "
                "nothing here is a judgement that the conflict is too hard. Every "
                "OTHER conflicted file this run resolved is in the merge it left "
                "behind.",
            )
        # Every harness cause is ruled out above, so these markers are what the model
        # decided about these hunks — a verdict a resolver fix does not re-open.
        # The prompt rides the model's DECLINE RECORD, never this branch alone:
        # markers reach here with no record when a generator did not re-derive
        # its file, and that reader is repairing, not deciding.
        said = _decline_reasons(marker_files).strip()
        refuse(
            "conflict markers still present in the tree",
            "the resolution left conflict markers behind."
            f"{_decline_reasons(marker_files)}",
            declined=True,
            escalate=escalation_block(marker_files, said) if said else "",
        )
