#!/usr/bin/env python3
"""Auto-resolve merge conflicts — DISCOVER step.

Emits the PRs the resolve job should process, as a compact JSON array of
``{number, head_ref, base_ref, head_sha}`` on ``$GITHUB_OUTPUT`` as ``prs=...``.

``_discover_refusals`` words every refusal; ``_discover_chain`` reads a chained child's comparison.

Scope mirrors the merge-conflict labeler: ``PR_NUMBER`` set considers that one PR, unset
scans every open PR, and only that push scan reaches a conflict introduced from underneath
a PR. Only PRs the resolver may touch are emitted: open, not a WIP draft, not a fork that
refuses maintainer edits (no push from here reaches it), not a stacked child, and either
CONFLICTING or holding a wedged merge-queue entry. A dependency bot's PR the bot STILL
MANAGES is excluded on its HEAD COMMIT's author, because that upkeep ends when anyone
else pushes.

Two filters bound the spend, both keyed to the PR's OWN activity rather than to
the clock: ``AUTO_RESOLVE_MAX_COMMIT_AGE_HOURS`` over the newest of the head commit and
the last return to ready-for-review, and a per-head attempt mark, which a catch-up run
drops with ``0`` and ``AUTO_RESOLVE_IGNORE_ATTEMPT_MARK``. A third filter is correctness,
not spend: a PR with a merge-queue entry the queue could still build is never emitted,
because a push would dequeue it.

The discover job checks out ``.github/scripts`` sparsely and runs on the system ``python3``,
so this module imports only the standard library, its siblings and ``_ci_retry``/``_pr_sweep``.

The knobs this module reads:

  * ``AUTO_RESOLVE_ATTEMPT_FLOOR_MINUTES`` — once the mark is this old, a base push after it re-enables the PR.
  * ``AUTO_RESOLVE_ATTEMPT_TTL_HOURS`` — how long the mark holds while the base does not move.
  * ``AUTO_RESOLVE_VERDICT_RETRY_HOURS`` — how long a paid verdict on one head holds before a moved base re-opens it; ``0`` holds it forever.
  * ``AUTO_RESOLVE_VERDICT_RETRIES`` — how many such verdicts one head may draw in total.
  * ``AUTO_RESOLVE_BASE_SHA`` — a commit to merge instead of the base branch's tip; the one PR is then taken only when that real merge conflicts, and every mark is keyed to the commit.
  * ``MAX_PASSES`` — re-queries of a mergeability GitHub has not settled; skipped for a PR the queue has wedged, because GitHub stops recomputing once the queue owns its entry.
"""

import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

# A separate process from bundle.py, so its own print-vs-inherited-subprocess
# ordering needs its own fix — see bundle.py's fuller PROBLEM CLASS comment
# beside its own `reconfigure` call. The guard is load-bearing there and here: a
# harness can swap in a capture object with no `reconfigure`, which a cast misses
# and which then raises at IMPORT, before any test body runs.
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(
        line_buffering=True
    )  # allow-stdio-swap: single-threaded CLI, reconfigured once at import before any work starts

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
from _ci_retry import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    base_delay,
    retry_max,
)
from _gh_rate_limit import budget_summary  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _pr_sweep import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PR_SWEEP_LIMIT_DEFAULT,
    JsonObject,
)
from _discover_gh import ScanGh  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _discover_pinned import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PinnedBaseRefused,
    probe_pinned_base,
)
from _discover_refusals import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Holds,
    report_refusals,
    summarize_the_death,
)
from _discover_resolver_change import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    UNREADABLE,
    newest_resolver_commit,
    resolver_change_source,
)
from _discover_types import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    KNOWN_MERGEABILITY,
    RELEASED_SUFFIX,
    UNREAD,
    DiscoverError,
    HeadCommit,
    PullRequest,
    QueueEntryState,
    _iso_to_epoch,
    _newest_status,
    _status_count,
)
from _handoff_cause import mark_context  # noqa: E402,I001  # pylint: disable=wrong-import-position

# The per-head ATTEMPT mark, written by mark-attempt.sh before a run spends, and
# cleared by a push to the head. These three are shared-names keys, not contexts:
# :meth:`Probes.context` keys each one to the run's `base-sha`.
ATTEMPT_MARK = "auto_resolve_attempt"

# The per-head HANDOFF mark, written by every refusal in _refusal.fail — the one
# exit bundle.py takes when it gives up on a resolution. It rides the same statuses
# read as the attempt mark, and holds with no floor and no TTL — see already_attempted.
# A moved base retires it once the retry window passes (:meth:`Probes._verdict_is_spent`).
HANDOFF_MARK = "auto_resolve_handoff"

# The per-head DECLINE mark, written by the one refusal that has ruled every harness
# cause out: the model read these hunks and left them. It holds like the handoff mark
# and, unlike it, survives a change to the resolver's own code — that change cannot
# alter what the model thought of the conflict, and retiring the two together re-bought
# one PR's identical refusal three times in a day. A push to the head clears it, and so
# does a moved base once the retry window passes (see :meth:`Probes._verdict_is_spent`).
DECLINED_MARK = "auto_resolve_declined"

# Not a branch name, so it cannot collide with one in the shared probe cache.
_RESOLVER_CACHE_KEY = "//resolver"


class Hold(Enum):
    """Which mark on a head stops the resolver taking it again, from :meth:`Probes.hold_on`."""

    NONE = "NONE"  # nothing holds this head
    ATTEMPT = "ATTEMPT"  # a run started here; the TTL and the floor clear it
    HANDOFF = "HANDOFF"  # the harness delivered nothing; a head push, a resolver change or a bounded retry clears it
    DECLINED = "DECLINED"  # the model refused these hunks; a head push or a bounded retry clears it


@dataclass(frozen=True)
class Config:  # pylint: disable=too-many-instance-attributes  # a parameter object, not a behavioral class
    """Every knob one scan reads, resolved once from the environment.

    A parameter object rather than a bag of module globals: the predicates below
    take the config they consult, so a caller cannot reach a knob the signature
    does not name."""

    repo: str
    output_path: str
    step_summary_path: str | None
    pr_number: str | None
    max_age_secs: int
    max_passes: int
    retry_delay_secs: float
    ignore_attempt_mark: bool
    attempt_ttl_secs: int
    attempt_floor_secs: int
    verdict_retry_secs: int
    verdict_retry_max: int
    sweep_limit: int
    retry_max: int
    retry_base_delay: float
    chained_children: str
    # The commit the caller pinned as the base side (the `base-sha` input), or
    # empty for the tip of each PR's base branch. Set, a PR is selected only when
    # merging THIS commit into its head conflicts, whatever GitHub reports.
    base_sha: str
    # Where the base repository is cloned from, for the real merge a pinned base
    # side is judged by.
    server_url: str

    @property
    def max_commit_age_hours(self) -> int:
        """The age window in the units the operator set it in, for the messages
        that quote it back. Derived rather than stored beside the seconds, so the
        two spellings of one knob cannot disagree."""
        return self.max_age_secs // 3600

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "Config":
        for required in ("REPO", "GH_TOKEN", "GITHUB_OUTPUT"):
            if not env.get(required):
                raise DiscoverError(f"{required} required", plain=True)
        # Validated in the order the shell script validated them, because each
        # message is an operator-facing contract and a run that fails two checks
        # must still name the first one.
        ttl_hours = _positive_int(
            env.get("AUTO_RESOLVE_ATTEMPT_TTL_HOURS") or "2",
            "AUTO_RESOLVE_ATTEMPT_TTL_HOURS must be a positive whole number of hours",
        )
        # How long a mark holds even after the base moves. The floor is what
        # bounds spend on a PR the resolver keeps failing on while the base is
        # busy: without it, every merge to the base buys another paid attempt.
        # Minutes, not hours: the window worth setting here is tens of minutes,
        # which an hours-only knob cannot express.
        if env.get("AUTO_RESOLVE_ATTEMPT_FLOOR_HOURS"):
            raise DiscoverError(
                "AUTO_RESOLVE_ATTEMPT_FLOOR_HOURS is retired; unset that repository "
                "variable and set AUTO_RESOLVE_ATTEMPT_FLOOR_MINUTES to the same window in minutes"
            )
        floor_minutes = _positive_int(
            env.get("AUTO_RESOLVE_ATTEMPT_FLOOR_MINUTES") or "20",
            "AUTO_RESOLVE_ATTEMPT_FLOOR_MINUTES must be a positive whole number of minutes",
        )
        age_hours = _whole_int(
            env.get("AUTO_RESOLVE_MAX_COMMIT_AGE_HOURS") or "24",
            "AUTO_RESOLVE_MAX_COMMIT_AGE_HOURS must be a whole number of hours",
        )
        # How long a paid verdict on one head holds before a MOVED BASE re-opens it,
        # and how many verdicts one head may draw in total. 0 hours restores the
        # permanent hold. The pair is what stops a verdict stranding a PR forever
        # while still refusing to re-buy the same merge hourly.
        verdict_retry_hours = _whole_int(
            env.get("AUTO_RESOLVE_VERDICT_RETRY_HOURS") or "6",
            "AUTO_RESOLVE_VERDICT_RETRY_HOURS must be a whole number of hours",
        )
        verdict_retry_max = _positive_int(
            env.get("AUTO_RESOLVE_VERDICT_RETRIES") or "3",
            "AUTO_RESOLVE_VERDICT_RETRIES must be a positive whole number",
        )
        sweep_limit = env.get("SWEEP_PR_LIMIT") or str(PR_SWEEP_LIMIT_DEFAULT)
        if not re.fullmatch(r"[0-9]+", sweep_limit):
            raise DiscoverError(
                f"auto-resolve-discover: SWEEP_PR_LIMIT='{sweep_limit}' is not an integer",
                plain=True,
            )
        base_sha = env.get("AUTO_RESOLVE_BASE_SHA") or ""
        if base_sha and not env.get("PR_NUMBER"):
            raise DiscoverError(
                "auto-resolve-discover: AUTO_RESOLVE_BASE_SHA pins the merge for ONE "
                "pull request, so it needs PR_NUMBER too",
                plain=True,
            )
        chained = env.get("AUTO_RESOLVE_CHAINED_CHILDREN") or CHAINED_ON
        if chained not in CHAINED_MODES:
            raise DiscoverError(
                "auto-resolve-discover: AUTO_RESOLVE_CHAINED_CHILDREN must be one of "
                f"{', '.join(sorted(CHAINED_MODES))}, got '{chained}'",
                plain=True,
            )
        return cls(
            repo=env["REPO"],
            output_path=env["GITHUB_OUTPUT"],
            # Absent off a runner, where the refusal report has no summary to reach.
            step_summary_path=env.get("GITHUB_STEP_SUMMARY") or None,
            pr_number=env.get("PR_NUMBER") or None,
            max_age_secs=age_hours * 3600,
            max_passes=int(env.get("MAX_PASSES") or "3"),
            retry_delay_secs=float(env.get("RETRY_DELAY_SECS") or "10"),
            # Only the exact string opens the bypass. Anything else — "false", "",
            # a typo — leaves the per-head mark enforcing, because this knob
            # restores the per-push resolve cost the mark exists to bound.
            ignore_attempt_mark=env.get("AUTO_RESOLVE_IGNORE_ATTEMPT_MARK") == "true",
            attempt_ttl_secs=ttl_hours * 3600,
            attempt_floor_secs=floor_minutes * 60,
            verdict_retry_secs=verdict_retry_hours * 3600,
            verdict_retry_max=verdict_retry_max,
            sweep_limit=int(sweep_limit),
            retry_max=retry_max(env),
            retry_base_delay=base_delay(env),
            chained_children=chained,
            base_sha=base_sha,
            server_url=env.get("GITHUB_SERVER_URL") or "https://github.com",
        )


# The two accepted number shapes, spelled as regexes rather than `str.isdigit`.
# `isdigit` is true for superscripts and for non-ASCII digit scripts, so it would
# accept a value `int()` then rejects — and it accepts a leading zero the shell
# form refused. A knob whose validator and parser disagree fails inside the run
# instead of at its own check.
_WHOLE = re.compile(r"[0-9]+")
_POSITIVE = re.compile(r"[1-9][0-9]*")

# What the scan does with a chained child whose head carries a merge its base
# lacks. `log` reports the ones it would take and still refuses them; `on`
# resolves them. `on` is the default: the widening ran its live cycle from
# 2026-08-11 to 2026-08-17, over which every chained child it reported stayed
# conflicted because nothing else resolves one. There is no third value: a mode
# that also refused would differ from `log` in nothing but a log line, and it
# would still pay the same comparison per chained PR.
CHAINED_LOG = "log"
CHAINED_ON = "on"
CHAINED_MODES = frozenset({CHAINED_LOG, CHAINED_ON})


def _whole_int(raw: str, message: str) -> int:
    if not _WHOLE.fullmatch(raw):
        raise DiscoverError(f"{message}, got '{raw}'.")
    return int(raw)


def _positive_int(raw: str, message: str) -> int:
    if not _POSITIVE.fullmatch(raw):
        raise DiscoverError(f"{message}, got '{raw}'.")
    return int(raw)


# ── The closed sum type ──────────────────────────────────────────────────────
#
# What happens to ONE candidate the emit filter already accepted. The probes
# below cost an API call each, so they run only on the few PRs everything else
# accepted — and each has its own verdict, which shell carried as two parallel
# arrays plus an accumulator. Here it is one value per candidate, and a caller
# cannot read a queued PR as an attempted one.


@dataclass(frozen=True)
class Eligible:
    """Emit this PR. Every filter cleared it."""

    pr: PullRequest


@dataclass(frozen=True)
class Queued:
    """The queue holds an entry it could still merge
    (:meth:`Probes.queue_state` answered ``PENDING``)."""

    pr: PullRequest


@dataclass(frozen=True)
class Attempted:
    """The resolver already ran against this head (:meth:`Probes.already_attempted`)."""

    pr: PullRequest


@dataclass(frozen=True)
class HandedOff:
    """A paid run reached a verdict on this head and left the rest to a human.

    Its own outcome rather than an :class:`Attempted`, because what clears the two
    differs: the attempt mark expires, and this one holds until the head moves or
    the resolver itself changes. Reported as one line for both is how a permanently
    stranded PR read exactly like one inside its floor."""

    pr: PullRequest


@dataclass(frozen=True)
class Unconfirmed:
    """Mergeability never settled and no wedged queue entry vouches for a conflict,
    so nothing here proves this PR needs resolving."""

    pr: PullRequest


CandidateOutcome = (
    Eligible | Queued | Attempted | HandedOff | Unconfirmed | PinnedBaseRefused
)


def classify_candidate(pr: PullRequest, probes: "Probes") -> CandidateOutcome:
    """Bucket ONE accepted candidate into the closed sum type above — the only
    classifier, so no caller decides for itself what kind of skip it saw.

    The order matches the probe costs, and the queue probe runs first even under
    a catch-up run: a catch-up that dequeues a green PR is the same incident the
    queue filter exists to prevent. The UNDECIDED arm is what reaches a PR wedged
    in the queue, and a WEDGED entry is the only positive evidence that such a PR
    really is conflicted — without one the scan cannot tell a conflict from a
    mergeability GitHub has simply not computed yet, so it declines to push.
    Everything the emit filter rejected never arrives, so there is no default arm.
    """
    state = probes.queue_state(pr.number)
    if state is QueueEntryState.PENDING:
        return Queued(pr)
    # GitHub's mergeability judges the head against its base BRANCH, so it says
    # nothing about a pinned base-side commit; the real merge below decides that.
    pinned = probes.config.base_sha
    if pr.is_undecided and state is not QueueEntryState.WEDGED and not pinned:
        return Unconfirmed(pr)
    held = probes.hold_on(pr)
    if held in (Hold.HANDOFF, Hold.DECLINED):
        return HandedOff(pr)
    if held is Hold.ATTEMPT:
        return Attempted(pr)
    if pinned and (verdict := probes.pinned_base_verdict(pr)) != "CONFLICTING":
        return PinnedBaseRefused(pr, verdict)
    return Eligible(pr)


@dataclass(frozen=True)
class Probes:
    """The two per-candidate probes, each one API call, plus the catch-up bypass
    and a per-base tip read shared across every candidate on that base."""

    gh: ScanGh
    config: Config
    # The per-run cache `base_moved_at` fills; a cached None is a read that
    # failed and stays failed for the run. Mutable inside a frozen record on
    # purpose: a cache is not identity, and freezing the fields above is what
    # the frozen decorator is for.
    _base_moves: dict[str, float | None] = field(default_factory=dict)

    def queue_state(self, number: int) -> QueueEntryState:
        """Which :class:`QueueEntryState` describes this PR's merge-queue entry —
        PENDING whenever the answer is unreadable.

        GraphQL, because ``gh``'s ``--json`` field set carries no queue state.
        Both fields come from the one call, so the UNMERGEABLE carve-out below
        costs no extra request."""
        try:
            answer = self.gh.run_gh(
                [
                    "api",
                    "graphql",
                    "-F",
                    f"owner={self.config.repo.split('/')[0]}",
                    "-F",
                    f"name={self.config.repo.split('/', 1)[1]}",
                    "-F",
                    f"number={number}",
                    "-f",
                    "query=query($owner: String!, $name: String!, $number: Int!) {\n"
                    "        repository(owner: $owner, name: $name) {\n"
                    "          pullRequest(number: $number) {\n"
                    "            isInMergeQueue\n"
                    "            mergeQueueEntry { state }\n"
                    "          }\n"
                    "        }\n"
                    "      }",
                    "--jq",
                    "[(.data.repository.pullRequest.isInMergeQueue | tostring), "
                    '(.data.repository.pullRequest.mergeQueueEntry.state // "")] '
                    "| @tsv",
                ],
                capture=True,
            ).strip()
        except DiscoverError:
            answer = ""
        queued, _, entry_state = answer.partition("\t")
        if queued == "true":
            if entry_state == "UNMERGEABLE":
                print(
                    f"PR #{number} holds an UNMERGEABLE queue entry — the queue "
                    "will never build it and never evict it, so a push costs no "
                    "merge.",
                    file=sys.stderr,
                )
                return QueueEntryState.WEDGED
            return QueueEntryState.PENDING
        if queued == "false":
            return QueueEntryState.ABSENT
        print(
            f"queue state unreadable for PR #{number} (the probe failed, or it "
            "answered null) — assuming it IS queued and leaving it alone (fail closed).",
            file=sys.stderr,
        )
        return QueueEntryState.PENDING

    def hold_on(self, pr: PullRequest) -> "Hold":
        """Which mark on this PR's head, if any, stops the resolver taking it.

        Fresh means: younger than the TTL, and — once past the floor — written
        after the base last moved. A base that moved since the mark changed the
        conflict the attempt failed on, so holding the mark would make the PR
        wait out a TTL for a retry that already has new information. A mark
        older than the TTL is treated as no mark: whatever the earlier run
        concluded, the code that concluded it may since have been fixed, and
        nothing else would ever retry this tree. A failed statuses read answers
        "not fresh" — the cost of a redundant attempt is one run, while wrongly
        reporting "fresh" would silently strand a head the resolver should
        handle. An unreadable BASE TIP goes the other way and holds: it is no
        evidence the base moved, holding strands nothing (the TTL still expires
        the mark), and retrying would turn one branch-read outage into a paid
        resolve for every marked PR in the scan."""
        if self.config.ignore_attempt_mark:
            return Hold.NONE
        try:
            # --paginate, because the cap below COUNTS marks: a head carrying more
            # statuses than one page would undercount them, and the count is what
            # bounds the paid retries.
            statuses = self.gh.api_json(
                f"repos/{self.config.repo}/commits/{pr.head_sha}/statuses?per_page=100",
                "--paginate",
            )
        except DiscoverError:
            return Hold.NONE
        marked = _newest_status(statuses, self.context(ATTEMPT_MARK))
        released = _newest_status(
            statuses, f"{self.context(ATTEMPT_MARK)}{RELEASED_SUFFIX}"
        )
        # A release stamped in the same second as the mark it cancels wins, for the
        # same reason the failed read does: the failure worth preventing is a head
        # nothing ever retries. Read BEFORE the handoff mark, because a free run writes
        # both — a ladder whose every rung was dead still reaches bundle, which refuses a
        # tree nothing resolved, so the release says that run bought nothing.
        if released >= marked:
            return Hold.NONE
        # A DECLINE is read first and takes no resolver-change test: it records what
        # the model decided about these hunks, and a resolver fix does not re-open
        # that. The newer-attempt guard below applies to it for the same reason.
        if (
            declined := _newest_status(statuses, self.context(DECLINED_MARK))
        ) and marked <= declined:
            if self._verdict_is_spent(statuses, declined, pr):
                return Hold.NONE
            return Hold.DECLINED
        # An attempt mark NEWER than the handoff belongs to a run that started
        # once a scan had already retired the mark — that run's own mark
        # governs, so this falls through to the ordinary ATTEMPT check below
        # rather than to a verdict this newer run never returned.
        if (
            handed_off := _newest_status(statuses, self.context(HANDOFF_MARK))
        ) and marked <= handed_off:
            if not self._verdict_still_stands(marked):
                return Hold.NONE
            if self._verdict_is_spent(statuses, handed_off, pr):
                return Hold.NONE
            return Hold.HANDOFF
        return (
            Hold.ATTEMPT
            if self._within_ttl_and_floor(marked, pr.base_ref)
            else Hold.NONE
        )

    def pinned_base_verdict(self, pr: PullRequest) -> str | None:
        """What a REAL merge of the pinned base-side commit into PR's head says.
        ``_discover_pinned.probe_pinned_base`` owns the merge and its docstring;
        last among the probes because it clones the base repository, which costs
        more than every read above it."""
        clone_url = f"{self.config.server_url}/{self.config.repo}.git"
        return probe_pinned_base(
            sys.executable, clone_url, pr.number, pr.base_ref, self.config.base_sha
        )

    def context(self, mark: str) -> str:
        """The status context MARK is read under for this scan's base side."""
        return mark_context(mark, self.config.base_sha)

    def _verdict_is_spent(
        self, statuses: object, verdict_at: float, pr: PullRequest
    ) -> bool:
        """Whether a paid verdict on this head has stopped describing the merge a
        retry would face, so this scan may buy one more.

        A verdict is about ONE merge: this head against the base as it stood. The
        base then moves, and the conflict the next run faces is a different one —
        so a verdict held forever strands a pull request nothing else resolves,
        which is what left three of this repository's own PRs conflicted for days.
        Three conditions bound the spend, and every one of them must hold:

        * the base MOVED since the verdict, so the retry has new information;
        * the verdict is older than ``AUTO_RESOLVE_VERDICT_RETRY_HOURS``, which
          caps a busy base at one retry per window rather than one per push;
        * this head has drawn fewer than ``AUTO_RESOLVE_VERDICT_RETRIES``
          verdicts of BOTH kinds together, which is what stops an unresolvable
          conflict billing forever — counting one kind alone would let a head
          alternating handoff and decline draw twice the advertised total.

        An unreadable base tip HOLDS the verdict, matching :meth:`base_moved_at`:
        it is no evidence the base moved, and retrying on one API outage would buy
        a paid resolve for every stranded PR in the scan at once."""
        # A pinned base side never moves, so no retry ever has new information.
        if self.config.verdict_retry_secs <= 0 or self.config.base_sha:
            return False
        drawn = _status_count(statuses, self.context(HANDOFF_MARK)) + _status_count(
            statuses, self.context(DECLINED_MARK)
        )
        if drawn >= self.config.verdict_retry_max:
            return False
        if verdict_at > time.time() - self.config.verdict_retry_secs:
            return False
        moved = self.base_moved_at(pr.base_ref)
        return moved is not None and moved > verdict_at

    def _verdict_still_stands(self, marked: float) -> bool:
        """Whether the handoff mark written by the run that started at MARKED
        still describes what a re-run would do.

        MARKED, not the handoff's own timestamp: the workflow stages the
        resolver, then marks the attempt, then spends the run's whole duration
        before writing the handoff — so a resolver change landing mid-run reads
        as "before the handoff" and the stale verdict never retires. Comparing
        against the attempt mark instead anchors on the moment closest to
        staging that this scan can read, in the same job the staging step ran.

        The mark takes neither the floor nor the TTL, because it records that a
        PAID run reached a verdict on this tree: a base push does not change the
        hunks the model declined, so re-enabling on one buys the identical verdict
        at full LLM cost — hourly, on a repository that merges to main dozens of
        times a day. A push to the head clears it.

        The RESOLVER'S OWN CODE is the other input, and the mark that ignored it
        stranded ten PRs. A run refused with the resolver as it stood; once that
        code changes, the mark is a verdict about a program that no longer runs,
        and nothing else in this repository would ever land those conflicts. An
        unreadable answer holds the mark, matching :meth:`base_moved_at`: it is no
        evidence of a change, and retrying on one API outage would buy a paid
        resolve for every stranded PR in the scan at once."""
        changed = self.resolver_changed_at()
        return changed is None or changed <= marked

    def resolver_changed_at(self) -> float | None:
        """When the resolver's own code last changed, as an epoch, or None when it
        cannot be read. Cached: every handed-off PR in the scan asks this."""
        if _RESOLVER_CACHE_KEY not in self._base_moves:
            self._base_moves[_RESOLVER_CACHE_KEY] = newest_resolver_commit(
                self._commit_date_at, self.config.repo
            )
        return self._base_moves[_RESOLVER_CACHE_KEY]

    def _commit_date_at(self, path: str) -> object:
        """The newest commit date PATH answers, as an epoch — None when it names no
        commit, and UNREADABLE when the read failed. The two must not collapse: the
        first says only this path is stale, the second holds every mark."""
        try:
            answer = self.gh.api_json(path)
        except DiscoverError:
            return UNREADABLE
        newest = answer[0] if isinstance(answer, list) and answer else answer
        meta = newest.get("commit") if isinstance(newest, dict) else None
        committer = meta.get("committer") if isinstance(meta, dict) else None
        date = committer.get("date") if isinstance(committer, dict) else None
        return _iso_to_epoch(date) if date else None

    def _within_ttl_and_floor(self, marked: float, base_ref: str) -> bool:
        """The floor/TTL/base-scoping rule for an ATTEMPT mark, once a handoff mark
        and a same-second release have already been ruled out."""
        if marked <= time.time() - self.config.attempt_ttl_secs:
            return False
        # A pinned base side never moves, so only the TTL retires its mark.
        if (
            marked > time.time() - self.config.attempt_floor_secs
            or self.config.base_sha
        ):
            return True
        moved = self.base_moved_at(base_ref)
        return moved is None or moved <= marked

    def base_moved_at(self, ref: str) -> float | None:
        """When branch REF last moved — its tip commit's committer date, as an
        epoch — or None when it cannot be read. Cached: every marked PR on the
        same base asks the same question. The committer date is stamped by the
        pusher's clock, not GitHub's, so a push carrying a backdated committer
        date reads as "not moved" and that mark degrades to the TTL-only hold;
        a future-dated one costs at most one extra attempt per floor."""
        if ref not in self._base_moves:
            try:
                answer = self.gh.api_json(f"repos/{self.config.repo}/branches/{ref}")
            except DiscoverError:
                answer = None
            tip = answer.get("commit") if isinstance(answer, dict) else None
            meta = tip.get("commit") if isinstance(tip, dict) else None
            committer = meta.get("committer") if isinstance(meta, dict) else None
            date = committer.get("date") if isinstance(committer, dict) else None
            self._base_moves[ref] = _iso_to_epoch(date) if date else None
        return self._base_moves[ref]


# ── Notices ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Notifier:
    """Posts one terminal notice per PR, at most once ever.

    Every caller is a TERMINAL state: the resolver will never pick these PRs up
    again, and nothing else in this repo lands their conflict — no workflow, no
    script, no cron. The only record was a line in a run log nobody opens, so the
    PR itself is where the notice has to go. The marker keeps it to one comment per
    PR; repeating it on every scan would be worse than silence."""

    gh: ScanGh
    config: Config

    def notify_once(self, number: int, marker: str, body: str) -> None:
        try:
            existing = self.gh.run_gh(
                [
                    "api",
                    "--paginate",
                    f"repos/{self.config.repo}/issues/{number}/comments",
                    "--jq",
                    ".[].body",
                ],
                capture=True,
            )
        except DiscoverError:
            print(
                f"::warning::could not read PR #{number}'s comments, so its "
                "auto-resolve notice was not posted."
            )
            return
        if marker in existing:
            return
        try:
            self.gh.run_gh(
                [
                    "api",
                    "--silent",
                    "--method",
                    "POST",
                    f"repos/{self.config.repo}/issues/{number}/comments",
                    "-f",
                    f"body={body}\n\n{marker}",
                ],
                capture=False,
            )
        except DiscoverError:
            print(f"::warning::could not post the auto-resolve notice on PR #{number}.")

    def notify_each(self, numbers: list[int], marker: str, body: str) -> None:
        for number in numbers:
            self.notify_once(number, marker, body)


# ── The scan ─────────────────────────────────────────────────────────────────


@dataclass
class Scan:
    """One discover run. Holds the state the passes share, so nothing is threaded
    through module globals."""

    config: Config
    gh: ScanGh
    candidates: list[PullRequest] = field(default_factory=list)
    open_heads: frozenset[str] = frozenset()
    # Every PR whose mergeability GitHub has already SETTLED this run, keyed by
    # number, as the whole facts dict that read carried. A later pass exists to
    # wait on the undecided ones, so it re-reads those and nothing else.
    settled: dict[int, JsonObject] = field(default_factory=dict)
    # Keyed by SHA, because a commit's date and author never change. A later pass
    # exists to wait on mergeability, so re-reading the same commit once per pass
    # per candidate buys nothing — three passes over this repo's 14 conflicted
    # PRs cost 42 reads for 14 commits.
    head_commits: dict[str, HeadCommit] = field(default_factory=dict)
    # Whether each chained child's head already carries a merge from its base,
    # keyed by PR number. One comparison per chained PR per scan, not per rail.
    # None is "the comparison could not be read", kept DISTINCT from False: only
    # False licenses the notice, which asserts the head carries no such merge.
    chain_verdicts: dict[int, bool | None] = field(default_factory=dict)

    def chain_is_resolvable(self, pr: PullRequest) -> bool:
        """True when this chained child is one the resolver may take.

        Only a head that already carries a merge from its base qualifies, so a
        chain that could still be a native stack is refused. The probe costs one
        API call and runs only on a chained PR, which is a handful per scan; its
        answer is memoised because both rails and the skip report ask it.
        """
        if pr.number not in self.chain_verdicts:
            self.chain_verdicts[pr.number] = self.gh.chain_carries_a_merge(
                pr.base_ref, pr.head_ref
            )
        return (
            self.chain_verdicts[pr.number] is True
            and self.config.chained_children == CHAINED_ON
        )

    def refused_chain(self, pr: PullRequest) -> bool:
        """A chained child this scan will not touch — the rails' spelling."""
        return pr.is_chained_child(self.open_heads) and not self.chain_is_resolvable(pr)

    def chain_held_by_the_knob(self, pr: PullRequest) -> bool:
        """A chained child this scan could take, and the mode did not let it."""
        return self.refused_chain(pr) and self.chain_verdicts.get(pr.number) is True

    def chain_unread(self, pr: PullRequest) -> bool:
        """A chained child whose comparison did not answer."""
        return self.refused_chain(pr) and self.chain_verdicts.get(pr.number) is None

    def reads_as_native_stack(self, pr: PullRequest) -> bool:
        """A chained child whose comparison SAID its head carries no merge the
        base lacks. The one arm that licenses the stacked notice, which is posted
        once and never retracted, so it must never stand in for an unread one."""
        return self.refused_chain(pr) and self.chain_verdicts.get(pr.number) is False

    def emittable(self, pr: PullRequest) -> bool:
        """Every rail the resolver must clear before it may touch a PR.

        A cross-repository head is refused because a fork's token is read-only
        and its author is untrusted. Every other bot-authored PR IS eligible —
        this repo's own automation opens most PRs, and the resolved head is
        re-validated by CI and human review before it can merge.

        An UNDECIDED PR is admitted here and refused in :func:`classify_candidate`
        unless a wedged queue entry vouches for it: a PR the queue has wedged never
        reads CONFLICTING, so demanding it at this rail would drop exactly the PRs
        the queue cannot heal on its own."""
        return (
            pr.is_open
            and not pr.is_wip_draft
            and not pr.is_unpushable_fork
            and self.may_need_a_merge(pr)
            and not pr.is_bot_managed
            and not self.refused_chain(pr)
            and not pr.is_blocked
            and not pr.is_template_sync
            and pr.within_age_window(self.config.max_age_secs)
        )

    def may_need_a_merge(self, pr: PullRequest) -> bool:
        """Whether this PR could have a conflict to resolve. With a pinned base
        side, GitHub's mergeability is about the wrong merge, so every PR could,
        and :func:`classify_candidate` decides it by merging for real."""
        return bool(self.config.base_sha) or pr.is_conflicting or pr.is_undecided

    def still_undecided(self, pr: PullRequest) -> bool:
        """A PR that could still flip to CONFLICTING and be emitted.

        Deliberately NOT gated on the opt-out label: a labelled PR is dropped from
        the emit set anyway, and waiting on its mergeability would burn a retry
        pass for a verdict nothing acts on."""
        return (
            pr.is_open
            and not pr.is_wip_draft
            and not pr.is_unpushable_fork
            and not pr.is_bot_managed
            and not self.refused_chain(pr)
            and pr.within_age_window(self.config.max_age_secs)
            and pr.is_undecided
            and not self.config.base_sha
        )

    def with_live_facts(self, prs: list[PullRequest]) -> list[PullRequest]:
        """Each candidate carrying the mergeability every rail and every skip
        report reads.

        A per-PR read inside a retry loop, which costs passes ×
        open PRs rather than open PRs. A verdict GitHub has already SETTLED is
        kept for the passes after it, because a later pass exists only to wait on
        the undecided ones. So three passes over 65 open PRs cost 65 reads plus
        one per undecided PR per extra pass, not 195.

        Every open row is read on the first pass, including one a rail will drop:
        the run log lists the conflicted PRs it skipped as stacked, opted out or
        out of the age window, and CONFLICTING is what puts a PR on those lists.
        Skipping the read for a row the listing alone refuses would save one read
        per WIP draft (0 of this repository's 50 open PRs on 2026-08-12, because
        every draft it holds sits on a session branch the cap parked) and cost
        those three reports their subject, which is a bad trade: the reports are
        how an operator learns a conflicted PR is being left alone, and the reads
        this scan needs to lose are counted in hundreds, not in nines.

        A PR-scoped row already carries GraphQL's own mergeability, so that value
        is left alone: that read is the one place a member this scan does not
        model can arrive. Its head SHA is replaced all the same.
        """
        return [self._read_live_facts(pr) for pr in prs]

    def _read_live_facts(self, pr: PullRequest) -> PullRequest:
        """This PR carrying the mergeability and the head SHA one REST read
        answered.

        A head SHA taken from a GraphQL read, which trails a push
        by minutes. Both rows this scan builds carry one: the open-PR listing and
        the single-PR `pr view`. So the correction is unconditional, where the
        mergeability half is not. The stale SHA skipped PR #4030 as already
        attempted, because the mark it matched sat on the head the resolver had
        indeed tried, and it would have emitted that same SHA for the checkout."""
        facts = self.settled.get(pr.number) or self.gh.pr_facts(pr.number)
        if facts["mergeable"] in ("MERGEABLE", "CONFLICTING"):
            self.settled[pr.number] = facts
        return replace(
            pr,
            mergeable=facts["mergeable"] if pr.mergeable == UNREAD else pr.mergeable,
            head_sha=facts["headRefOid"],
        )

    def with_activity_dates(self, prs: list[PullRequest]) -> list[PullRequest]:
        """Attach the dates the age window reads to each candidate that could
        still be emitted — and leave the rest with none.

        A MERGEABLE PR is dropped before the window is ever read, so it is not
        fetched: the extra calls are bounded by the number of conflicted or
        undecided PRs, not by the repo's open-PR count."""
        return [
            self._dated_candidate(pr)
            if pr.mergeable != "MERGEABLE" or self.config.base_sha
            else pr
            for pr in prs
        ]

    def _dated_candidate(self, pr: PullRequest) -> PullRequest:
        """One candidate carrying every activity date the window needs.

        The head-commit read is unconditional; the ready-for-review read runs
        only when the commit date alone would drop the PR, so the second call
        costs nothing on a PR that is inside the window already.

        It is also skipped for every PR :meth:`emittable` refuses on a fact about
        that PR ALONE — a WIP draft, a fork, a dependency bot's, an opted-out one. No
        date can make one of those emittable, so the read would buy a value no
        predicate acts on, once per candidate per retry pass. ``is_chained_child``
        is deliberately absent: it needs ``open_heads``, which this pass has not
        computed yet."""
        if pr.head_sha not in self.head_commits:
            self.head_commits[pr.head_sha] = self.gh.head_commit(pr.head_sha)
        pr = pr.with_head_commit(self.head_commits[pr.head_sha])
        if self._refused_whatever_its_dates(pr) or pr.within_age_window(
            self.config.max_age_secs
        ):
            return pr
        stamp, read_failed = self.gh.ready_for_review_date(pr.number)
        return pr.with_activity_date(stamp, read_failed=read_failed)

    @staticmethod
    def _refused_whatever_its_dates(pr: PullRequest) -> bool:
        """The :meth:`emittable` rails that read this PR and nothing else."""
        return (
            pr.is_wip_draft
            or pr.is_unpushable_fork
            or pr.is_bot_managed
            or pr.is_blocked
            or pr.is_template_sync
        )

    def conflicted(self, keep) -> list[int]:
        """The open conflicted PR numbers KEEP accepts, in listing order. With a
        pinned base side GitHub's verdict is about another merge, so that is every
        open PR KEEP accepts."""
        return [
            pr.number
            for pr in self.candidates
            if pr.is_open and (pr.is_conflicting or self.config.base_sha) and keep(pr)
        ]

    def otherwise_eligible(self, pr: PullRequest) -> bool:
        """A PR the resolver would otherwise have taken.

        Each notice claims one named reason for why the resolver stopped. A PR the
        resolver drops for a DIFFERENT reason as well — a WIP draft, a fork PR, a
        dependency-bot PR, an opted-out PR — would get a comment naming the wrong
        cause, and acting on it would not help. So a notice goes only to a PR this
        accepts. The run-log lists stay wide, because a log costs nobody a comment."""
        return (
            not pr.is_wip_draft
            and not pr.is_unpushable_fork
            and not pr.is_bot_managed
            and not pr.is_blocked
            and not pr.is_template_sync
        )

    def fork_head_is_the_only_bar(self, pr: PullRequest) -> bool:
        """A fork PR the resolver would otherwise have taken.

        `otherwise_eligible` counts the fork head as a reason of its own, so the
        same test with that one field cleared says whether the fork head is the
        WHOLE cause — which is what the notice claims."""
        return pr.is_unpushable_fork and self.otherwise_eligible(
            replace(pr, maintainer_can_modify=True)
        )

    def collect(self) -> list[PullRequest]:
        """Run the retry passes and return the PRs the emit filter accepts.

        GitHub computes mergeability lazily, so a candidate that is neither
        MERGEABLE nor CONFLICTING is re-queried until it settles or the passes run
        out. Only an eligible-but-undecided PR holds the loop: one that is out of
        the window, stacked, or bot-authored is not going to be emitted however its
        mergeability settles, so waiting on it would just burn the passes."""
        # In single-PR mode the one `pr view` carries no sibling heads, so the
        # stacked-child check needs its own open-PR listing; a failed listing fails
        # the scan rather than silently resolving a stack child. The push-scan
        # listing already carries every open head, so it is re-read per pass.
        if self.config.pr_number:
            self.open_heads = self.gh.open_head_refs()
        emitted: list[PullRequest] = []
        for pass_number in range(1, self.config.max_passes + 1):
            if pass_number > 1:
                time.sleep(self.config.retry_delay_secs)
            self.candidates = self.with_activity_dates(
                self.with_live_facts(self.gh.scoped_prs())
            )
            if not self.config.pr_number:
                self.open_heads = frozenset(pr.head_ref for pr in self.candidates)
            emitted = [pr for pr in self.candidates if self.emittable(pr)]
            if not any(self.still_undecided(pr) for pr in self.candidates):
                break
        return emitted


def report_unrecognized_mergeability(candidates: list[PullRequest]) -> None:
    """Say so when GitHub reported a mergeability this scan does not model.

    An unmodelled value is treated as undecided, so the scan re-queries it every
    pass and then drops the PR unless a wedged queue entry vouches for it. Both
    outcomes read in the log exactly like a genuinely-undecided PR's, so the PR
    can never be resolved and drops quietly on every scan forever with nothing
    naming the reason. Retrying is still the safe reading of an unknown answer;
    what this changes is that somebody learns the set needs a new member."""
    unrecognized = sorted(
        {pr.mergeable for pr in candidates if pr.mergeable not in KNOWN_MERGEABILITY}
    )
    if unrecognized:
        print(
            f"::warning::GitHub reported mergeability {', '.join(unrecognized)}, "
            "which auto-resolve does not model. Those PRs are treated as undecided, "
            "so this scan drops each one that holds no wedged queue entry. Add the "
            "value to KNOWN_MERGEABILITY in "
            "auto-resolve/discover.py once its meaning is settled."
        )


def _emit_entry(pr: PullRequest) -> JsonObject:
    """The record the resolve and land jobs consume.

    The head SHA is here for the resolve job's concurrency key. Keying that group
    on the PR NUMBER makes a re-scan of a head a resolve is ALREADY working on
    cancel that resolve, so on a base branch that advances faster than a resolve
    takes, no resolve ever finishes."""
    return {
        "number": pr.number,
        "head_ref": pr.head_ref,
        "head_repo": pr.head_repo,
        "base_ref": pr.base_ref,
        "head_sha": pr.head_sha,
    }


def run(config: Config) -> None:
    """One discover run, from the listing to the written output."""
    gh = ScanGh(
        repo=config.repo,
        pr_number=config.pr_number,
        sweep_limit=config.sweep_limit,
        retry_max=config.retry_max,
        retry_base_delay=config.retry_base_delay,
    )
    scan = Scan(config, gh)
    probes = Probes(gh, config)
    notifier = Notifier(gh, config)

    accepted = scan.collect()
    report_unrecognized_mergeability(scan.candidates)

    if config.ignore_attempt_mark:
        print(
            "AUTO_RESOLVE_IGNORE_ATTEMPT_MARK=true — re-running against heads the "
            "resolver already attempted."
        )

    outcomes = [classify_candidate(pr, probes) for pr in accepted]
    eligible = [o.pr for o in outcomes if isinstance(o, Eligible)]
    queued = [o.pr.number for o in outcomes if isinstance(o, Queued)]
    attempted = [o.pr.number for o in outcomes if isinstance(o, Attempted)]
    handed_off = [o.pr.number for o in outcomes if isinstance(o, HandedOff)]
    unconfirmed = [o.pr.number for o in outcomes if isinstance(o, Unconfirmed)]
    pinned_base = [
        (o.pr.number, o.verdict) for o in outcomes if isinstance(o, PinnedBaseRefused)
    ]

    refusals = report_refusals(
        scan,
        notifier,
        Holds(unconfirmed, queued, attempted, handed_off, pinned_base),
        resolver_change_source(config.repo),
    )

    prs = json.dumps([_emit_entry(pr) for pr in eligible], separators=(",", ":"))
    print(f"Auto-resolve will process: {prs}")
    print(
        f"auto-resolve-discover: spent {gh.calls} GitHub API calls this scan "
        f"over {len(scan.candidates)} open PR(s)."
    )
    # stderr, and its own line: the budget carries a live reset stamp, so it is
    # the one part of this report that cannot be compared against a golden
    # record — and it is a diagnostic about the run, not the scan's answer.
    print(f"auto-resolve-discover: budget left — {budget_summary()}.", file=sys.stderr)
    # An OUTPUT rather than a raise: the step that tells the PR why the run
    # resolved nothing runs only after a successful discover, so raising would
    # trade a wrong run status for a PR that never learns the reason. The
    # workflow fails the job on this output, once that comment is posted.
    unread = refusals.blocking_read_failures()
    with open(config.output_path, "a", encoding="utf-8") as handle:
        handle.write(f"prs={prs}\n")
        handle.write(f"read_failed={'true' if not eligible and unread else 'false'}\n")
        handle.writelines(refusals.output_lines(config.pr_number))
    refusals.write_step_summary(config.step_summary_path)


def main() -> None:
    try:
        run(Config.from_env(dict(os.environ)))
    except DiscoverError as error:
        prefix = "" if getattr(error, "plain", False) else "::error::"
        print(f"{prefix}{error}", file=sys.stderr)
        summarize_the_death(
            os.environ.get("GITHUB_STEP_SUMMARY"), str(error), budget_summary()
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
