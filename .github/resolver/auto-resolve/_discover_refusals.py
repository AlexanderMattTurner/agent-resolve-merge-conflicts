"""Every refusal the auto-resolve DISCOVER step makes, and the surfaces each one reaches.

PROBLEM CLASS — a job that reports success while refusing the work it was asked for. A
reason printed only to stdout leaves a maintainer reading a green run and a pull request
with nothing on it. So each reason is worded ONCE here and reaches three places: the run
log, the run's step summary, and — for a scan scoped to one PR — the step outputs the
workflow posts as a comment on that PR.

Three of these refusals also post a notice on the pull request, because no later scan
takes it until someone acts: a fork head with maintainer edits off, a native stack, and
the age window. The marker holds each to ONE comment per PR, rewritten when its wording
changes rather than repeated.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _discover_types import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PR_LABEL_AUTO_RESOLVE_BLOCKED,
    PR_LABEL_TEMPLATE_SYNC,
)

STACKED_MARKER = "<!-- auto-resolve-stacked-child -->"
AGED_OUT_MARKER = "<!-- auto-resolve-aged-out -->"
FORK_HEAD_MARKER = "<!-- auto-resolve-fork-head -->"

FORK_HEAD_BODY = (
    "⚠️ **Auto-resolve cannot push to this PR's branch.** Its head lives in a "
    'fork, and "Allow edits by maintainers" is off, so the resolver cannot deliver '
    "the merge it would make. Turn that setting on and the next scan resolves the "
    "conflict. Leave it off and no automation will: merge the base branch into "
    "your branch by hand, resolve the conflicts, and push the merge commit."
)

STACKED_BODY = (
    "⚠️ **Auto-resolve will not touch this PR.** Its base is another open PR's "
    "head, and this head carries no merge commit that base lacks — so the chain "
    "still "
    "reads as a native stack, whose linear history a resolver merge commit would "
    "break. No other automation resolves this conflict. Resolve it yourself, and "
    "how depends on which shape the chain is:\n"
    "- **A native stack** — rebase it. Use the merge box's **Rebase stack** "
    "button, or run `gh stack rebase` and then `gh stack push`.\n"
    "- **A manual chain** — a human pointed one PR's base at another PR's "
    "branch. There is no stack, so `gh stack` cannot help: where the repository "
    "does not enable stacks it answers `Stacked PRs are not enabled for this "
    "repository`. Merge the base branch into the head branch by hand, resolve "
    "the conflicts, and push the merge commit."
)


def aged_out_body(hours: int) -> str:
    return (
        "⚠️ **Auto-resolve has stopped watching this PR.** Neither its newest "
        "commit nor any return to ready-for-review the scan could read is inside "
        f"the {hours}h auto-resolve window "
        "(`AUTO_RESOLVE_MAX_COMMIT_AGE_HOURS`). Every later scan drops it for the "
        "same reason, so the conflict stays until you act. Push a commit to bring "
        "the branch back inside the window, or merge the base branch in by hand."
    )


def _render(numbers: list[int]) -> str:
    """The bracketed, comma-joined number list every skip line reports."""
    return "[" + ",".join(str(n) for n in numbers) + "]"


def _one_line(text: str) -> str:
    """TEXT with every run of whitespace collapsed to one space.

    INVARIANT — a reason crossing `$GITHUB_OUTPUT` holds no newline. That channel
    is line-oriented, so a newline truncates the value and re-parses the tail as
    output commands of its own choosing."""
    return " ".join(text.split())


@dataclass(frozen=True)
class Refusal:
    """One rail's refusal: the PRs it held back, and the text that says why."""

    rail: str
    numbers: tuple[int, ...]
    text: str


class Refusals:
    """Every refusal one scan made, in the order the rails reported them."""

    def __init__(self) -> None:
        self.entries: list[Refusal] = []

    def refuse(self, numbers: list[int], rail: str, text: str) -> None:
        """Record RAIL's refusal of NUMBERS, and log it.

        The print stays here so the log line and every other surface carry the
        same bytes: a summary that paraphrases its log line is a second message
        to keep current."""
        self.entries.append(Refusal(rail, tuple(numbers), text))
        print(text)

    def output_lines(self, pr_number: str | None) -> list[str]:
        """The `$GITHUB_OUTPUT` lines a one-PR scan writes, which the workflow reads
        to post that PR's refusal on it. Empty for a push scan: the pair names the
        one refusal to publish, and a push scan refuses many PRs."""
        scoped = self._scoped(pr_number)
        if not scoped:
            return []
        return [
            f"refused_rail={','.join(entry.rail for entry in scoped)}\n",
            "refused_reason="
            + _one_line(" ".join(entry.text for entry in scoped))
            + "\n",
        ]

    def _scoped(self, pr_number: str | None) -> list["Refusal"]:
        """Every refusal a one-PR scan publishes on that PR, in print order.

        ALL of them, never just the first: one PR can trip several filters, and a
        comment naming one implies its remedy is the whole remedy. A fork carrying
        the opt-out label would otherwise be told to remove the label, which
        re-enables nothing while the fork rail still holds it."""
        if pr_number is None:
            return []
        return [
            entry
            for entry in self.entries
            if pr_number in {str(number) for number in entry.numbers}
        ]

    def write_step_summary(self, path: str | None) -> None:
        """Add every refusal to the run's step summary — the surface a maintainer
        reads without opening the log. PATH is absent outside Actions, where there
        is no summary to write."""
        if not path or not self.entries:
            return
        lines = ["### Auto-resolve refused"]
        lines += [f"- `{entry.rail}` — {entry.text}" for entry in self.entries]
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


@dataclass(frozen=True)
class Holds:
    """The four refusals a per-PR classification decides, as PR-number lists."""

    unconfirmed: list[int]
    queued: list[int]
    attempted: list[int]
    handed_off: list[int]


def report_refusals(
    scan, notifier, holds: Holds, resolver_change_source: str
) -> Refusals:
    """Report every PR this scan refused, and notify the ones nothing will lift.

    SCAN and NOTIFIER are discover.py's own; they are taken as arguments rather
    than imported, so the wording lives here and the scanning stays there."""
    config = scan.config
    refusals = Refusals()
    if holds.unconfirmed:
        refusals.refuse(
            holds.unconfirmed,
            "mergeability-unknown",
            f"Skipping PR(s) {_render(holds.unconfirmed)} — GitHub has not computed "
            "their mergeability and no wedged queue entry vouches for a conflict, "
            "so nothing proves they need resolving. A later scan picks them up "
            "once mergeability settles.",
        )
    if holds.queued:
        refusals.refuse(
            holds.queued,
            "merge-queue",
            f"Skipping PR(s) {_render(holds.queued)} — currently in the merge queue; a "
            "resolver push would dequeue them. The scan after their queue entry "
            "settles picks them up.",
        )
    if holds.attempted:
        refusals.refuse(
            holds.attempted,
            "already-attempted",
            f"Skipping PR(s) {_render(holds.attempted)} — auto-resolve already ran "
            "against the current head commit; a head push re-enables it now, "
            "and a base push does once the mark outlives the floor.",
        )
    if holds.handed_off:
        refusals.refuse(
            holds.handed_off,
            "handed-off",
            f"Skipping PR(s) {_render(holds.handed_off)} — a paid resolve reached a "
            "verdict on the current head and left the rest to a human. Neither "
            "the floor nor the TTL clears this. A moved base re-opens it once "
            "AUTO_RESOLVE_VERDICT_RETRY_HOURS passes, up to "
            "AUTO_RESOLVE_VERDICT_RETRIES verdicts per head. Sooner: push to the "
            "branch, dispatch auto-resolve-conflicts.yaml with catch-up=true, or "
            f"move the resolver's own code — {resolver_change_source}.",
        )

    blocked = scan.conflicted(lambda pr: pr.is_blocked)
    if blocked:
        refusals.refuse(
            blocked,
            "blocked-label",
            f"Skipping {PR_LABEL_AUTO_RESOLVE_BLOCKED} PR(s) {_render(blocked)} — "
            "remove the label to re-enable auto-resolve for them.",
        )

    template_sync = scan.conflicted(lambda pr: pr.is_template_sync)
    if template_sync:
        refusals.refuse(
            template_sync,
            "template-sync-label",
            f"Skipping {PR_LABEL_TEMPLATE_SYNC} PR(s) {_render(template_sync)} — "
            "its diff is the whole synced template, and a conflict against a "
            "moved base needs a human's read of it, not a paid LLM merge.",
        )

    # A fork head is refused for one of two causes, and only ONE asks its author
    # for anything. The refusal earns the notice, because no later scan takes the
    # PR until that setting changes. The unread answer earns none: the next scan
    # retries the read, so a notice would name a bar that may not exist.
    fork_refused = scan.conflicted(lambda pr: pr.fork_edits_refused)
    if fork_refused:
        refusals.refuse(
            fork_refused,
            "fork-edits-refused",
            f"Skipping fork PR(s) {_render(fork_refused)} — auto-resolve pushes "
            "the resolved merge to the pull request's own branch, and this "
            'repository may not write it. Enable "Allow edits by maintainers" on '
            "the pull request and the next scan resolves the conflict.",
        )
        # Only where the fork head is the WHOLE cause, which is what the notice
        # claims: a PR also held by its age or its draft state would read the
        # notice as the one thing to fix and still be dropped.
        notifier.notify_each(
            scan.conflicted(
                lambda pr: pr.fork_edits_refused and scan.fork_head_is_the_only_bar(pr)
            ),
            FORK_HEAD_MARKER,
            FORK_HEAD_BODY,
        )

    fork_unread = scan.conflicted(lambda pr: pr.fork_edits_unread)
    if fork_unread:
        refusals.refuse(
            fork_unread,
            "fork-edits-unread",
            f"Skipping fork PR(s) {_render(fork_unread)} — whether this "
            "repository may write their branch could not be read, so nothing "
            "says a resolution could be delivered. A later scan retries the read.",
        )

    # Two reasons a chained PR is refused, and they need separate reports: the
    # knob held a PR this scan could have taken, or the chain still reads as a
    # native stack. Only the second earns the notice, which is posted once and
    # never retracted — sending it to a PR whose head demonstrably carries a
    # merge would leave a false reason standing for every later reader.
    held = scan.conflicted(scan.chain_held_by_the_knob)
    if held:
        refusals.refuse(
            held,
            "chained-child-knob",
            f"Chained PR(s) {_render(held)} carry a merge commit their base lacks, "
            "so they are not native stacks and this scan could resolve them. "
            f"AUTO_RESOLVE_CHAINED_CHILDREN is '{scan.config.chained_children}', "
            "so it did not.",
        )

    unread = scan.conflicted(scan.chain_unread)
    if unread:
        refusals.refuse(
            unread,
            "chain-comparison-unread",
            f"Skipping chained PR(s) {_render(unread)} — the comparison that would "
            "say whether their head carries a merge their base lacks could not be "
            "read, so this scan cannot rule out a native stack.",
        )

    stacked = scan.conflicted(scan.reads_as_native_stack)
    if stacked:
        refusals.refuse(
            stacked,
            "stacked-child",
            f"Skipping stacked PR(s) {_render(stacked)} — base is another open "
            "PR's head and the head carries no merge its base lacks, so this may "
            "be a native stack, whose cascading rebase owns these conflicts.",
        )
        # The notice asserts the head carries no such merge, so only a comparison
        # that SAID so may post it. An unread comparison leaves the PR refused and
        # silent — the warning above is the record, and a later scan posts the
        # notice once the read succeeds.
        notifier.notify_each(
            scan.conflicted(
                lambda pr: (
                    scan.reads_as_native_stack(pr) and scan.otherwise_eligible(pr)
                )
            ),
            STACKED_MARKER,
            STACKED_BODY,
        )

    aged_out = scan.conflicted(lambda pr: not pr.within_age_window(config.max_age_secs))
    if aged_out:
        refusals.refuse(
            aged_out,
            "aged-out",
            f"Skipping PR(s) {_render(aged_out)} — no commit, and no readable "
            f"return to ready-for-review, in the last {config.max_commit_age_hours}h; "
            "outside the auto-resolve window (AUTO_RESOLVE_MAX_COMMIT_AGE_HOURS).",
        )
        notifier.notify_each(
            scan.conflicted(
                lambda pr: (
                    not pr.within_age_window(config.max_age_secs)
                    and scan.otherwise_eligible(pr)
                )
            ),
            AGED_OUT_MARKER,
            aged_out_body(config.max_commit_age_hours),
        )
    return refusals
