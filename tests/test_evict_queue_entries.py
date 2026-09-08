""".github/resolver/evict-queue-entries.sh — the merge-queue entry evictor.

The labeler names the PRs whose conflict makes their queue entry unbuildable;
this script drops those entries. It runs in the one job holding `contents:
write`, so what it must never do is act on doubt: GitHub answering anything but
a plain `true` to "is this PR queued" leaves the entry alone.

Drives the real script against a stub `gh` on PATH that answers the membership
query from $GH_IN_MERGE_QUEUE and records every call, so each case below asserts
the mutation made — or, just as load-bearing, not made.

Non-vacuity: dropping the `((rc == 0)) || continue` guard makes the unqueued and
unreadable cases dequeue, and both go red here.
"""

# covers: .github/resolver/evict-queue-entries.sh
# covers: .github/resolver/lib/pr-merge-queue.bash

from pathlib import Path

import pytest

from tests._helpers import (
    REPO_ROOT,
    current_path,
    run_capture,
    write_exe,
)

SCRIPT = REPO_ROOT / ".github" / "resolver" / "evict-queue-entries.sh"

# Newlines collapsed: a GraphQL document arrives as ONE argument spanning many
# lines, and an unflattened log would record that one call as many.
GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "${*//$'\n'/ }" >>"$GH_LOG"
# The membership read. Empty models GitHub answering nothing — the doubt the
# script declines to act on, distinct from a plain "false".
if [[ "$*" == *"isInMergeQueue"* ]]; then
  printf '%s\n' "${GH_IN_MERGE_QUEUE:-}"
  exit 0
fi
# The node id the mutation addresses the PR by.
if [[ "$*" == *"pullRequest.id"* ]]; then
  printf 'PR_node_id\n'
  exit 0
fi
# The eviction itself. GH_DEQUEUE_FAILS models a mutation GitHub refuses.
if [[ "$*" == *"dequeuePullRequest"* ]]; then
  [[ -z "${GH_DEQUEUE_FAILS:-}" ]] || exit 1
  printf '{"data":{"dequeuePullRequest":{"mergeQueueEntry":null}}}\n'
  exit 0
fi
# The sticky-comment listing. GH_COMMENT_IDS names the ids it answers, so "no
# notice yet" and "one already posted" are distinct inputs rather than a default.
if [[ "$*" == *"--paginate"* && "$*" == *"/comments"* ]]; then
  [[ -z "${GH_COMMENT_LIST_FAILS:-}" ]] || exit 1
  [[ -z "${GH_COMMENT_IDS:-}" ]] || printf '%s\n' "$GH_COMMENT_IDS"
  exit 0
fi
# A sticky another run deleted between the listing and this write. gh renders the
# status as "(HTTP 404)", which is the string gh_unless_gone matches on.
if [[ "$*" == *"-X PATCH"* && -n "${GH_PATCH_GONE:-}" ]]; then
  echo "gh: Not Found (HTTP 404)" >&2
  exit 1
fi
exit 0
"""


def _run_evictor(
    tmp_path: Path, evict_prs: str, **extra_env: str
) -> tuple[list[str], str]:
    """Run the real script with the stub gh; return (recorded gh calls, output)."""
    stub_dir = tmp_path / "bin"
    write_exe(stub_dir / "gh", GH_STUB)
    log = tmp_path / "gh.log"
    log.touch()
    env = {
        "PATH": f"{stub_dir}:{current_path()}",
        "GH_LOG": str(log),
        "REPO": "owner/repo",
        "EVICT_PRS": evict_prs,
        "RETRY_MAX": "1",
        "RETRY_BASE_DELAY": "0",
        **extra_env,
    }
    result = run_capture(["bash", str(SCRIPT)], env=env)
    assert result.returncode == 0, result.stderr
    return log.read_text(encoding="utf-8").splitlines(), result.stdout + result.stderr


@pytest.mark.parametrize(
    ("in_queue", "evicted"),
    [
        # The queue holds an entry for a PR that conflicts with its base, so the
        # entry can never build and nothing but this drops it.
        ("true", True),
        # No entry to drop.
        ("false", False),
        # GitHub answered nothing, so membership is unknown — never guess: the
        # cost of a wrong dequeue is a PR's auto-merge arming, and the cost of a
        # wrong skip is one more scan.
        ("", False),
    ],
)
def test_only_a_queued_pr_loses_its_entry(
    tmp_path: Path, in_queue: str, evicted: bool
) -> None:
    calls, output = _run_evictor(tmp_path, "#7", GH_IN_MERGE_QUEUE=in_queue)
    assert any("dequeuePullRequest" in c for c in calls) is evicted, calls
    assert ("evicted PR #7's merge-queue entry" in output) is evicted, output


def test_every_named_pr_is_visited(tmp_path: Path) -> None:
    """A scan names its whole conflicted set in one output, so a loop that stopped
    at the first entry would leave every later one holding its slot in silence."""
    calls, _output = _run_evictor(tmp_path, "#7 #12", GH_IN_MERGE_QUEUE="true")
    ids = [c for c in calls if "pullRequest.id" in c]
    assert sum("number=7" in c for c in ids) == 1, calls
    assert sum("number=12" in c for c in ids) == 1, calls


def test_a_refused_eviction_leaves_a_notice_a_human_can_read(tmp_path: Path) -> None:
    """The queue neither builds nor drops the entry, so it holds a slot until
    somebody removes it by hand — and a ::warning:: reaches only a run log."""
    calls, output = _run_evictor(
        tmp_path, "#7", GH_IN_MERGE_QUEUE="true", GH_DEQUEUE_FAILS="1"
    )
    posted = [c for c in calls if "issues/7/comments" in c and "-F body=@" in c]
    assert posted, calls
    assert "::warning::merge-queue entries left in place for #7" in output, output


def test_a_refused_eviction_edits_the_notice_it_already_left(tmp_path: Path) -> None:
    calls, _output = _run_evictor(
        tmp_path,
        "#7",
        GH_IN_MERGE_QUEUE="true",
        GH_DEQUEUE_FAILS="1",
        GH_COMMENT_IDS="4242",
    )
    assert any("-X PATCH" in c and "issues/comments/4242" in c for c in calls), calls
    assert not any("issues/7/comments -F body=@" in c for c in calls), (
        "posted a second notice instead of editing the first"
    )


def test_a_later_eviction_deletes_the_notice_it_settles(tmp_path: Path) -> None:
    """Left standing, the notice keeps telling every later reader the PR holds a
    queue slot it no longer has."""
    calls, _output = _run_evictor(
        tmp_path, "#7", GH_IN_MERGE_QUEUE="true", GH_COMMENT_IDS="4242"
    )
    assert any("-X DELETE" in c and "issues/comments/4242" in c for c in calls), calls


def test_a_sticky_deleted_mid_write_costs_only_that_notice(tmp_path: Path) -> None:
    """The id comes from a listing and is used a round trip later, so a concurrent
    run that deletes the sticky makes the PATCH 404. Aborting there would leave every
    PR after this one holding its entry, and drop the warning naming the stuck set —
    the failure landing hardest on exactly the path this notice exists to report."""
    calls, output = _run_evictor(
        tmp_path,
        "#7 #12",
        GH_IN_MERGE_QUEUE="true",
        GH_DEQUEUE_FAILS="1",
        GH_COMMENT_IDS="4242",
        GH_PATCH_GONE="1",
    )
    patched = [c for c in calls if "-X PATCH" in c]
    assert len(patched) == 2, calls
    assert "::warning::merge-queue entries left in place for #7 #12" in output, output
    # A sticky already gone is the state a sticky wants, so it is not a failure to
    # publish one. Reporting it would send a person after a notice nobody needs.
    assert "could not be published" not in output, output


def test_an_unreadable_comment_listing_costs_only_that_notice(tmp_path: Path) -> None:
    """The listing is the one answer that must never read as "no comment" — taking it
    for one posts a duplicate every broken-token run. It is still only a comment, so
    the PRs after this one keep their evictions and the stuck set is still named."""
    calls, output = _run_evictor(
        tmp_path,
        "#7 #12",
        GH_IN_MERGE_QUEUE="true",
        GH_DEQUEUE_FAILS="1",
        GH_COMMENT_LIST_FAILS="1",
    )
    assert not any("-X PATCH" in c or "-F body=@" in c for c in calls), calls
    assert output.count("could not be published") == 2, output
    assert "::warning::merge-queue entries left in place for #7 #12" in output, output


def test_an_unparsable_token_is_named_and_costs_no_api_call(tmp_path: Path) -> None:
    """The labeler builds this list, so a token that is not a number means the two
    sides disagree on the format — a silent skip would hide that for good."""
    calls, output = _run_evictor(tmp_path, "#7 oops", GH_IN_MERGE_QUEUE="true")
    assert "::warning::ignoring 'oops'" in output, output
    assert not any("oops" in c for c in calls), calls
    assert any("dequeuePullRequest" in c for c in calls), calls


def test_an_empty_list_touches_nothing(tmp_path: Path) -> None:
    """The job's `if` already gates on a non-empty list, but the script is also a
    dispatchable entry point: an empty EVICT_PRS must not read a PR named ''."""
    calls, output = _run_evictor(tmp_path, "")
    assert calls == [], calls
    assert "::warning::" not in output, output
