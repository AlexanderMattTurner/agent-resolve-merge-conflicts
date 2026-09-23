#!/usr/bin/env python3
"""The gh CLI seam the auto-resolve DISCOVER step calls through.

Every request this scan makes to GitHub runs through :class:`ScanGh`, with the
shared retry and a call count discover.py reports as the scan's own share of
the installation's hourly API budget.

An underscore filename so ``discover.py`` can ``import`` it outright: the
hyphenated scripts beside it load each other through ``importlib``, and a type
reached that way is a runtime attribute pyright cannot resolve in an
annotation. :class:`ScanGh` takes its retry bounds and scope as plain fields
rather than ``discover.Config`` itself, which would import back and cycle.
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import NoReturn

from _ci_retry import Backoff, with_retry
from _discover_chain import COMPARE_PAGE, carries_a_merge
from _discover_types import _EPOCH, DiscoverError, HeadCommit, PullRequest
from _pr_sweep import JsonObject, read_mergeability

# The `gh pr list --json` field set the scan reads. `commits` is deliberately
# absent: it pulls each commit's `authors` connection, so GitHub's node estimate
# for the listing is PRs x commits x authors — 200 x 250 x 100 blows past the
# 500,000-node ceiling and the whole sweep dies, taking every push-scan discovery
# down with it. The head commit's date and author are fetched per candidate
# instead, in one read.
LISTING_FIELDS = (
    "number,mergeable,isDraft,isCrossRepository,maintainerCanModify,"
    "headRepositoryOwner,headRepository,headRefName,"
    "headRefOid,baseRefName,state,labels,author"
)

# What the OPEN-PR listing asks for: the same set without the one field whose
# cost is per open PR. Derived, so a field added above reaches both listings.
OPEN_LISTING_FIELDS = ",".join(
    field_name for field_name in LISTING_FIELDS.split(",") if field_name != "mergeable"
)


@dataclass
class ScanGh:
    """Every call this scan makes to the GitHub CLI, with the shared retry.

    A flaky network step (an API 5xx blip) is re-tried with exponential backoff,
    while a genuine failure still exhausts the cap and raises — fail loud.

    Not :class:`_pr_sweep.Gh`, the sweeps' general runner: this one takes its
    retry bounds from ``discover.Config``'s own fields rather than the
    environment, raises :class:`DiscoverError`, and leaves stderr on the
    process's own channel.

    Every call is counted. The count is the only way to see this scan's share of
    the installation's hourly API budget from the run log, and a scan that spends
    it is what silences the resolver: an exhausted budget fails discover, and
    resolve and land are then skipped, so the sweep resolves nothing (run
    31555882659, 2026-08-12 02:07Z).
    """

    repo: str
    pr_number: str | None
    sweep_limit: int
    retry_max: int
    retry_base_delay: float
    calls: int = 0

    def run_gh(self, args: list[str], *, capture: bool) -> str:
        """Run one ``gh`` call, re-running on nonzero exit with exponential
        backoff. Raises :class:`DiscoverError` once the cap is exhausted, so a
        failed read can never degrade into an empty result the caller reads as a
        clean repo."""
        shown = " ".join(["gh", *args])

        def once() -> subprocess.CompletedProcess:
            # Counted here, not beside the with_retry call: a retried read spends
            # a REQUEST per attempt, and a retry is what happens when the budget
            # is under pressure — which is the situation this count is for.
            self.calls += 1
            done = subprocess.run(
                ["gh", *args],
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
            )
            # Captured so the retry can read GitHub's refusal out of it, and
            # echoed unchanged so the run log reads as it did when gh wrote
            # straight to this process's stderr.
            if done.stderr:
                print(done.stderr, end="", file=sys.stderr)
            return done

        def give_up() -> NoReturn:
            raise DiscoverError(f"gh call failed: {shown}", plain=True)

        done = with_retry(
            shown,
            once,
            give_up,
            Backoff(maximum=self.retry_max, delay=self.retry_base_delay),
        )
        return done.stdout if capture else ""

    def api_json(self, path: str, *extra: str) -> object:
        return json.loads(self.run_gh(["api", path, *extra], capture=True) or "null")

    def scoped_prs(self) -> list[PullRequest]:
        """The PR rows for the current scope: with ``PR_NUMBER`` set the one PR it
        names, else every open PR.

        One scope switch, so an event-scoped run and a full sweep hand their caller
        the same shape."""
        if self.pr_number:
            raw = self.run_gh(
                [
                    "pr",
                    "view",
                    self.pr_number,
                    "--repo",
                    self.repo,
                    "--json",
                    LISTING_FIELDS,
                ],
                capture=True,
            )
            return [PullRequest.from_listing(json.loads(raw))]
        return [
            PullRequest.from_listing(row)
            for row in self.open_listing(OPEN_LISTING_FIELDS)
        ]

    def open_head_refs(self) -> frozenset[str]:
        """Every open PR's head ref name, the set the stacked-child test reads.

        Asks for ONE field, so this listing stays far under GitHub's node ceiling.
        It returns raw rows rather than :class:`PullRequest` values on purpose: a
        one-field row cannot populate a record whose other fields are required,
        and a record with invented defaults would answer questions it never read."""
        return frozenset(row["headRefName"] for row in self.open_listing("headRefName"))

    def _pull(self, number: int) -> JsonObject:
        """One PR's REST object, which mergeability rides in one computation."""
        return json.loads(
            self.run_gh(["api", f"repos/{self.repo}/pulls/{number}"], capture=True)
        )

    def chain_carries_a_merge(self, base_ref: str, head_ref: str) -> bool | None:
        """Does this chain's head hold a merge commit the base does not?

        `_discover_chain` owns the answer; this supplies the pages. A failed read
        answers None rather than raising: it decides ONE chained PR, and `run_gh`
        has already exhausted its retries, so letting it end the scan would drop
        every other candidate over a PR the rail refuses anyway."""
        span = f"{base_ref}...{head_ref}"
        path = f"repos/{self.repo}/compare/{span}"

        def read_page(page: int) -> str | None:
            try:
                query = f"per_page={COMPARE_PAGE}&page={page}"
                return self.run_gh(["api", f"{path}?{query}"], capture=True)
            except DiscoverError:
                print(f"::warning::could not compare {span}.")
                return None

        return carries_a_merge(read_page, span)

    def pr_facts(self, number: int) -> JsonObject:
        """This PR's mergeability and its head SHA, in GraphQL's spellings, from
        ONE PR's read.

        The listing cannot answer the mergeability: asking GitHub to compute it
        for every open PR at once is what it answers 502 to. It answers the head
        SHA, but from a GraphQL listing that lags a push, so the authoritative
        one rides back on this same read rather than costing a second."""
        return read_mergeability("auto-resolve-discover", number, self._pull)

    def open_listing(self, fields: str) -> list[JsonObject]:
        rows = self._one_listing(fields)
        listed = len(rows)
        # A full page means the repo may have more open PRs than this sweep can
        # see, so the excess would silently never be swept. Fail loud (warn) rather
        # than quietly under-sweep — no silent caps.
        if listed >= self.sweep_limit:
            print(
                f"::warning::auto-resolve-discover: open-PR page hit the "
                f"{self.sweep_limit} cap; PRs beyond this are not swept. "
                "Raise SWEEP_PR_LIMIT or paginate.",
                file=sys.stderr,
            )
        return rows

    def _one_listing(self, fields: str) -> list[JsonObject]:
        """One ``gh pr list`` page of this repository's open PRs."""
        raw = self.run_gh(
            [
                "pr",
                "list",
                "--repo",
                self.repo,
                "--state",
                "open",
                "--limit",
                str(self.sweep_limit),
                "--json",
                fields,
            ],
            capture=True,
        )
        return json.loads(raw)

    def head_commit(self, sha: str) -> "HeadCommit":
        """The head commit's committer date and author — one un-paginated read with
        no ceiling, which is what the age window asks for (see LISTING_FIELDS).

        Both facts come from the SAME read, so keying the bot-managed test on the
        head commit costs no extra request. An unattributed commit (an author email
        matching no GitHub account) answers the empty string, which no bot login
        equals."""
        raw = self.run_gh(
            [
                "api",
                f"repos/{self.repo}/commits/{sha}",
                "--jq",
                '{date: .commit.committer.date, author: (.author.login // "")}',
            ],
            capture=True,
        )
        row = json.loads(raw)
        return HeadCommit(row["date"], row["author"])

    def ready_for_review_date(self, number: int) -> tuple[str, bool]:
        """When this PR last came back from draft to ready-for-review, or the
        epoch when it never has.

        The scan cannot see a draft, and this repo drafts PRs that are merely
        over the ready cap (`cap-ready-prs.yaml`), so a wait for a free slot
        would spend the age window on a PR whose author did nothing wrong. The
        cap drafts and readies the same PR repeatedly, so only the NEWEST such
        event describes it now. A failed read answers the epoch rather than
        raising: the window then falls back to the head-commit date alone, which
        is what a scan that never asked would do — a probe outage must not widen
        the window."""
        try:
            raw = self.run_gh(
                [
                    "api",
                    "--paginate",
                    f"repos/{self.repo}/issues/{number}/timeline?per_page=100",
                    "--jq",
                    # `and .created_at` because a stamp-less entry would answer
                    # the literal `null`, which `_iso_to_epoch` raises on — that
                    # would take the whole scan down, not just this PR.
                    '.[] | select(.event == "ready_for_review" and .created_at)'
                    " | .created_at",
                ],
                capture=True,
            )
        except DiscoverError:
            print(
                f"::warning::could not read PR #{number}'s ready-for-review "
                "history; judging its age on the head commit alone.",
                file=sys.stderr,
            )
            return _EPOCH, True
        stamps = raw.split()
        return (max(stamps) if stamps else _EPOCH), False
