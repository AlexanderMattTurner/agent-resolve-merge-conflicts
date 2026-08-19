#!/usr/bin/env python3
"""Auto-resolve merge conflicts — OUTCOME GATE.

PROBLEM CLASS — a run that left the conflict standing and reported success. A
green run reaches no failure route: `ci-failure-notify.yaml` sees nothing, the
sticky PR comment is the only record, and the next run overwrites it. Three
defects had that one shape: a stale attempt mark made every later run a green
no-op (#4505), a `not_landed` verdict concluded success (#4426), and a
rate-limit death published nothing at all (#4481).

This module is the ONE place that decides what a run DID. Both jobs report their
facts and this function reads them, so no step re-derives the verdict and the
land job cannot disagree with the resolve job about whether the conflict is
still there.

A verdict is a STALL when the conflict is still there AND nothing else is on the
hook for it. Those exit non-zero. The three endings that are not stalls despite
pushing nothing each name who carries it instead: another live run
(``duplicate``), a fresh run already dispatched (``superseded``), or the merge
queue (``held``).

Standard library only: the land job checks the resolver out and runs the
runner's own python3, before any project install.
"""

import os
import sys
from dataclasses import dataclass
from enum import Enum


class Claim(Enum):
    """What the attempt mark said when this run asked to spend."""

    NONE = "none"  # the mark step never ran
    OWNED = "owned"  # this run holds the head
    DUPLICATE = "duplicate"  # another run holds it and is still in flight
    LATCHED = "latched"  # the mark is held by a run that has already concluded


class Published(Enum):
    """The terminal verdict the resolve job put on the pull request, if any."""

    NONE = "none"
    NO_OP = "no_op"  # one branch already contains the other
    HANDOFF = "handoff"  # the model resolved what it could, a human owns the rest
    DECLINE = "decline"  # the model read the conflict and refused it


class Land(Enum):
    """How the land job ended. ``land.sh`` writes exactly one of these."""

    NOT_RUN = "not_run"
    PUSHED = "pushed"
    NO_BUNDLE = "no_bundle"  # the resolve produced no resolution to push
    SUPERSEDED = "superseded"  # the branch moved; a later run takes the new head
    NOT_NEEDED = "not_needed"  # the branch moved and no longer conflicts
    QUEUE_HELD = "queue_held"  # pushing would eject the PR from the merge queue
    FAILED = "failed"  # land.sh refused or the push was rejected


@dataclass(frozen=True)
class RunFacts:
    """What the two jobs observed, each written by the step that observed it."""

    selected: bool
    claim: Claim
    published: Published
    land: Land

    @classmethod
    def from_env(cls) -> "RunFacts":
        """The facts as the workflow passes them. An unset value is the
        never-reached member, so a job that died early reads as one that got no
        further — never as one that succeeded."""
        return cls(
            selected=os.environ.get("SELECTED") == "true",
            claim=Claim(os.environ.get("CLAIM") or "none"),
            published=Published(os.environ.get("PUBLISHED") or "none"),
            land=Land(os.environ.get("LAND") or "not_run"),
        )


@dataclass(frozen=True)
class Verdict:
    """What the run did, whether that leaves the conflict stalled, and the
    sentence the run log carries."""

    name: str
    stall: bool
    sentence: str


# Read in order. The first arm that matches decides, so the order IS the
# precedence: who else is on the hook outranks what this run managed to do.
def verdict(facts: RunFacts) -> Verdict:
    """What FACTS mean. Total over the four enums — every combination lands on
    exactly one arm, which is what lets the TLA+ model enumerate them."""
    if not facts.selected:
        return Verdict(
            "refused",
            False,
            "discover did not select this pull request, so this run took no "
            "conflict on. Its refusal is on the pull request.",
        )
    if facts.claim is Claim.DUPLICATE:
        return Verdict(
            "duplicate",
            False,
            "another run holds this head's attempt mark and is still in flight, "
            "so that run reports this conflict.",
        )
    if facts.claim is Claim.LATCHED:
        return Verdict(
            "latched",
            True,
            "this head's attempt mark is held by a run that has already "
            "concluded, and the head carries no resolution. Nothing retries "
            "before the mark ages out.",
        )
    if facts.land is Land.PUSHED:
        return Verdict("landed", False, "the resolved merge is on the branch.")
    if facts.land is Land.SUPERSEDED:
        return Verdict(
            "superseded",
            False,
            "the branch moved while this resolution ran, so a later run takes "
            "the new head.",
        )
    if facts.land is Land.NOT_NEEDED:
        return Verdict(
            "already_clear", False, "the branch no longer conflicts with its base."
        )
    if facts.land is Land.QUEUE_HELD:
        return Verdict(
            "held",
            False,
            "the pull request entered the merge queue, which owns it until it leaves.",
        )
    if facts.land is Land.FAILED:
        return Verdict(
            "land_failed",
            True,
            "the land job refused this resolution, so the conflict is still there.",
        )
    if facts.published is Published.NO_OP:
        return Verdict(
            "no_op", False, "there was no merge to make, so nothing was resolved."
        )
    if facts.published in (Published.HANDOFF, Published.DECLINE):
        return Verdict(
            "handed_off",
            True,
            "this run published a verdict asking a human to resolve the "
            "conflict, so it is still there.",
        )
    return Verdict(
        "gave_up",
        True,
        "this run took the conflict on and ended with no resolution to push.",
    )


def main() -> None:
    """Print the verdict and exit non-zero on a stall.

    The exit status is what routes a stall to the failure notifier, so a run
    that resolved nothing cannot report success.
    """
    found = verdict(RunFacts.from_env())
    if not found.stall:
        print(f"auto-resolve outcome: {found.name} — {found.sentence}")
        return
    print(
        f"::error::auto-resolve outcome: {found.name} — {found.sentence}",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    main()
