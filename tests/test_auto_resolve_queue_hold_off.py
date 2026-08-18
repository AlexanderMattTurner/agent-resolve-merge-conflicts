"""The resolver never pushes over a merge-queue entry the queue could still build.

A push to a queued PR's head removes its entry, so resolving one converts "about
to merge" into "back of the line" — the paid run buys a lost queue slot. The one
carve-out is an entry GitHub judged UNMERGEABLE: the queue never builds it and
never evicts it, so nothing is lost by pushing and the deadlock is what a push
ends.

`discover.Probes.queue_state` is the single place that decision is made, and it
ships from this repository, so the property is checked here. Each case below sets
the GitHub wire answer, runs the REAL scan through the real `gh` binary against a
fake GitHub, and reads whether the PR was emitted. "Could still build" is decided
from GitHub's own semantics, not from the classifier, so a classifier that stops
holding a buildable entry off reds here.

The last case carries no entry state at all: the probe cannot read the answer.
There the resolver assumes the PR IS queued, because a wrong guess the other way
spends a slot.
"""

# covers: .github/resolver/auto-resolve/discover.py
# covers: .github/resolver/auto-resolve/_discover_types.py

from dataclasses import dataclass
from pathlib import Path

import pytest

from tests._fake_github import FakeResolverGitHub, ResolverPR, _queue_membership_reply
from tests._resolver_helpers import load_script

_discover_types = load_script(".github/resolver/auto-resolve/_discover_types.py")
QueueEntryState = _discover_types.QueueEntryState


class _UnreadableQueueGitHub(FakeResolverGitHub):
    """A server whose queue probe answers `null` — the shape gh returns when the
    query resolves no pull request. Modelled here rather than in the shared fake
    because only this suite asks what the resolver does with an unreadable
    answer."""

    def _merge_queue_reply(self, number: int):
        return _queue_membership_reply(None)


@dataclass(frozen=True)
class QueueCase:
    """One merge-queue wire answer, and what it means for the resolver.

    `buildable` is GitHub's own semantics, stated independently of the code under
    test: the queue builds and merges any entry it holds except an UNMERGEABLE
    one, and it holds no entry for a PR that is not queued.
    """

    name: str
    server: type[FakeResolverGitHub]
    queued: bool
    unmergeable: bool
    buildable: bool
    state: QueueEntryState


CASES = (
    QueueCase(
        name="no_entry",
        server=FakeResolverGitHub,
        queued=False,
        unmergeable=False,
        buildable=False,
        state=QueueEntryState.ABSENT,
    ),
    QueueCase(
        name="entry_the_queue_can_build",
        server=FakeResolverGitHub,
        queued=True,
        unmergeable=False,
        buildable=True,
        state=QueueEntryState.PENDING,
    ),
    QueueCase(
        name="unmergeable_entry",
        server=FakeResolverGitHub,
        queued=True,
        unmergeable=True,
        buildable=False,
        state=QueueEntryState.WEDGED,
    ),
    QueueCase(
        name="unreadable_answer",
        server=_UnreadableQueueGitHub,
        queued=False,
        unmergeable=False,
        # Unknown, so the resolver must treat it as buildable: the cost of
        # pushing over a live entry outweighs one skipped resolution.
        buildable=True,
        state=QueueEntryState.PENDING,
    ),
)


def _scan(case: QueueCase, tmp_path: Path) -> tuple[list[int], str, str]:
    """The PR numbers one real scan emits for `case`, plus both streams."""
    with case.server(tmp_path, [ResolverPR(1, head_ref="f1")]) as gh:
        if case.queued:
            gh.in_merge_queue.add(1)
        if case.unmergeable:
            gh.unmergeable_queue_entries.add(1)
        res = gh.discover()
        assert res.returncode == 0, res.stderr
        return [entry["number"] for entry in gh.emitted], res.stdout, res.stderr


def test_every_queue_entry_state_has_a_driven_case():
    """The classifier's state set is closed, so a member added with no case here
    would leave the resolver's behaviour on it unstated and still green."""
    assert {case.state for case in CASES} == set(QueueEntryState)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_the_resolver_holds_off_exactly_on_a_buildable_entry(case, tmp_path):
    emitted, stdout, stderr = _scan(case, tmp_path)
    if case.buildable:
        assert emitted == [], (
            f"{case.name}: the queue could still build this entry, and a push "
            "would send it to the back of the line"
        )
        assert "currently in the merge queue" in stdout
        return
    assert emitted == [1], (
        f"{case.name}: nothing the queue will build is at stake, so declining "
        "leaves the conflict standing"
    )
    assert "currently in the merge queue" not in stdout
    if case.state is QueueEntryState.WEDGED:
        # A wedged entry holds its slot forever and push-locks the branch, so a
        # scan that took it without a word reads exactly like one that skipped it.
        assert "UNMERGEABLE queue entry" in stderr
