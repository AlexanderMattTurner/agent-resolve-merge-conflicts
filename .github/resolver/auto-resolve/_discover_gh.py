"""Every GitHub read the auto-resolve DISCOVER step makes, and the retry each takes.

PROBLEM CLASS — a failed API read that degrades into an empty result. A scan that
reads "no open pull requests" out of a 502 emits nothing. Its caller cannot tell that
from a repository holding no conflicts. So every read here goes through one runner: it
re-tries a flaky call, raises :class:`DiscoverError` once the cap is exhausted, and
counts what it spent of the installation's hourly API budget.

Split out of ``discover.py``, which imports every name back. The boundary is a
repository and a number in, a fact out. Nothing here decides whether a pull request is
eligible, and nothing in ``discover.py`` runs ``gh``.
"""

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
from _ci_retry import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Backoff,
    with_retry,
)
from _pr_sweep import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    JsonObject,
    read_mergeability,
)
from _discover_types import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    DiscoverError,
    HeadCommit,
    PullRequest,
    _EPOCH,
)


class GhKnobs(Protocol):
    """The five knobs this layer reads off ``discover.py``'s ``Config``.

    Named here rather than imported: ``Config`` resolves the whole scan's
    environment, so importing it back would make the two modules circular. Spelling
    only what this layer reads is also what stops it reaching a knob its own
    signature does not name."""

    repo: str
    pr_number: str | None
    sweep_limit: int
    retry_max: int
    retry_base_delay: float


# The `gh pr list --json` field set the scan reads. `commits` is deliberately
# absent: it pulls each commit's `authors` connection, so GitHub's node estimate
# for the listing is PRs x commits x authors — 200 x 250 x 100 blows past the
# 500,000-node ceiling and the whole sweep dies, taking every push-scan discovery
# down with it. The head commit's date and author are fetched per candidate
# instead, in one read.
# `headRepository`/`headRepositoryOwner` are single objects, so they add two nodes per PR.
LISTING_FIELDS = (
    "number,mergeable,isDraft,isCrossRepository,headRefName,headRefOid,"
    "baseRefName,state,labels,author,headRepository,headRepositoryOwner"
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
    retry bounds from :class:`Config` rather than the environment, raises
    :class:`DiscoverError`, and leaves stderr on the process's own channel.

    Every call is counted. The count is the only way to see this scan's share of
    the installation's hourly API budget from the run log, and a scan that spends
    it is what silences the resolver: an exhausted budget fails discover, and
    resolve and land are then skipped, so the sweep resolves nothing (run
    31555882659, 2026-08-12 02:07Z).
    """

    config: GhKnobs
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
            Backoff(maximum=self.config.retry_max, delay=self.config.retry_base_delay),
        )
        return done.stdout if capture else ""

    def api_json(self, path: str, *extra: str) -> object:
        return json.loads(self.run_gh(["api", path, *extra], capture=True) or "null")

    def scoped_prs(self) -> list[PullRequest]:
        """The PR rows for the current scope: with ``PR_NUMBER`` set the one PR it
        names, else every open PR.

        One scope switch, so an event-scoped run and a full sweep hand their caller
        the same shape."""
        if self.config.pr_number:
            raw = self.run_gh(
                [
                    "pr",
                    "view",
                    self.config.pr_number,
                    "--repo",
                    self.config.repo,
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
            self.run_gh(
                ["api", f"repos/{self.config.repo}/pulls/{number}"], capture=True
            )
        )

    def chain_carries_a_merge(self, base_ref: str, head_ref: str) -> bool | None:
        """Does this chain's head hold a merge commit the base does not?

        None when the comparison could not be read or did not cover the range,
        which the caller treats as a refusal — a chain this scan cannot
        characterise keeps the old behaviour.

        A native stack requires fully linear history between its layers, so a head
        carrying ANY merge the base lacks is not one, whatever its shape suggests.
        That makes the answer a sound test for "landing one more merge commit here
        breaks nothing", and it needs no stacked-PR API: `compare` serves each
        commit's parents, and a commit with two is a merge.
        """
        path = f"repos/{self.config.repo}/compare/{base_ref}...{head_ref}"
        try:
            raw = self.run_gh(["api", f"{path}?per_page=100"], capture=True)
        except DiscoverError:
            # Caught rather than propagated: this read decides ONE chained PR, and
            # `run_gh` has already exhausted its retries. Letting it end the scan
            # would drop every other candidate over a PR the rail refuses anyway.
            print(f"::warning::could not compare {base_ref}...{head_ref}.")
            return None
        payload = json.loads(raw)
        commits = payload.get("commits", [])
        total = payload.get("total_commits", len(commits))
        if len(commits) < total:
            # This refusal is what keeps a truncated page from answering False.
            # `compare` serves commits oldest-first and pages only under
            # `--paginate`, so a chain more than one page ahead of its base hides
            # exactly the newest commits — where a merge from the base sits — and
            # a False here would post the notice below about a head that has one.
            print(
                f"::warning::comparison {base_ref}...{head_ref} listed "
                f"{len(commits)} of {total} commits."
            )
            return None
        return any(len(commit.get("parents", ())) >= 2 for commit in commits)

    def pr_facts(self, number: int) -> JsonObject:
        """This PR's mergeability, its head SHA and its maintainer-edits flag, in
        GraphQL's spellings, from ONE PR's read.

        The listing cannot answer the mergeability: asking GitHub to compute it
        for every open PR at once is what it answers 502 to. It answers the head
        SHA, but from a GraphQL listing that lags a push, so the authoritative
        one rides back on this same read rather than costing a second.
        ``maintainer_can_modify`` rides back too, so the fork rail costs no
        request of its own; a payload without the key answers None, which it
        refuses."""
        pulls: list[JsonObject] = []

        def read(pr_number: int) -> JsonObject:
            pulls.append(self._pull(pr_number))
            return pulls[-1]

        facts = read_mergeability("auto-resolve-discover", number, read)
        return facts | {"maintainerCanModify": pulls[-1].get("maintainer_can_modify")}

    def open_listing(self, fields: str) -> list[JsonObject]:
        rows = self._one_listing(fields)
        listed = len(rows)
        # A full page means the repo may have more open PRs than this sweep can
        # see, so the excess would silently never be swept. Fail loud (warn) rather
        # than quietly under-sweep — no silent caps.
        if listed >= self.config.sweep_limit:
            print(
                f"::warning::auto-resolve-discover: open-PR page hit the "
                f"{self.config.sweep_limit} cap; PRs beyond this are not swept. "
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
                self.config.repo,
                "--state",
                "open",
                "--limit",
                str(self.config.sweep_limit),
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
                f"repos/{self.config.repo}/commits/{sha}",
                "--jq",
                '{date: .commit.committer.date, author: (.author.login // "")}',
            ],
            capture=True,
        )
        row = json.loads(raw)
        return HeadCommit(row["date"], row["author"])

    def ready_for_review_date(self, number: int) -> str:
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
                    f"repos/{self.config.repo}/issues/{number}/timeline?per_page=100",
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
            return _EPOCH
        stamps = raw.split()
        return max(stamps) if stamps else _EPOCH
