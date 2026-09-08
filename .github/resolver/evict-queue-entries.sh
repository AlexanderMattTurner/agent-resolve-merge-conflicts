#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite: the suite runs the real
#   script as `bash <script>` against stubbed CLIs on PATH, so no run is ever traced.
# Drop the merge-queue entry each PR named in EVICT_PRS still holds.
#
# PROBLEM CLASS — the queue never evicts an entry it cannot merge. A conflicted
# PR's entry either holds a slot while the queue builds a merge that cannot
# exist, or holds the UNMERGEABLE state that build produces, which the queue
# neither builds nor drops. Both wait on a person until this runs.
#
# Eviction also releases the auto-resolver: it stands off a PR whose entry the
# queue could still build, because a push would eject that entry. With the entry
# gone, the next scan resolves the conflict instead of standing off it.
#
# Only a positive "is queued" answer acts, so an unreadable one leaves the entry
# alone and the next scan re-asks. One cost: a dequeue drops the PR's auto-merge
# arming. auto-resolve.yaml's land job pays the same cost; a repository running a
# re-arm sweep restores the arming, and one without it leaves that to a person.
#
# Env: GH_TOKEN, REPO, EVICT_PRS (label-merge-conflicts.sh's `evict-queue`).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/resolver/lib-ci-retry.sh
source "$SCRIPT_DIR/lib-ci-retry.sh"
# shellcheck source=.github/resolver/lib/pr-merge-queue.bash
source "$SCRIPT_DIR/lib/pr-merge-queue.bash"
# shellcheck source=.github/resolver/lib-marker-comment.sh
source "$SCRIPT_DIR/lib-marker-comment.sh"

REPO="${REPO:?REPO is required}"
STUCK_MARKER='<!-- merge-queue-entry-stuck -->'

# stuck_notice NUM — keep ONE comment naming an entry this run could not drop.
# The entry holds a queue slot until somebody removes it by hand, and a
# ::warning:: reaches only a run log nobody opens.
stuck_notice() {
  local num="$1" id rc=0 body
  local endpoint="repos/$REPO/issues/$num/comments"
  body="$(mktemp)"
  {
    printf '%s\n\n' "$STUCK_MARKER"
    printf 'This pull request conflicts with its base branch, so the merge queue can never build the entry it holds — and this run could not drop that entry.\n\n'
    printf 'Remove it in the merge queue UI. Until then it keeps a queue slot on a merge that cannot exist.\n'
  } >"$body"
  # A listing that could not be READ is not "no comment": treating it as one
  # posts a fresh notice on every broken-token run. Leave the sticky alone.
  id="$(marker_owned_comment_id "$endpoint" "$STUCK_MARKER")" || rc=$?
  if ((rc == 0)); then
    if [[ -n "$id" ]]; then
      patch_comment_if_changed "repos/$REPO/issues/comments/$id" "$body"
    else
      retry gh api "$endpoint" -F body=@"$body" >/dev/null
    fi
  fi
  rm -f "$body"
}

# clear_stuck_notice NUM — delete that notice once the entry is gone, so it stops
# telling every later reader the PR holds a queue slot it does not. Unguarded by a
# label, unlike the labeler's own sticky: only a conflicted and queued PR reaches
# here, which is already the small set a label would have named.
clear_stuck_notice() {
  local num="$1" raw id rc=0
  local -a ids=()
  raw="$(marker_owned_comment_ids "repos/$REPO/issues/$num/comments" "$STUCK_MARKER")" || rc=$?
  # A wrong answer here leaves a stale sticky one run longer, so it defers rather
  # than failing the run for every other PR in it.
  ((rc == 0)) || return 0
  [[ -z "$raw" ]] || mapfile -t ids <<<"$raw"
  for id in "${ids[@]}"; do
    [[ -n "$id" ]] || continue
    # 2 is "already gone", which is the state this wants anyway.
    gh_unless_gone api -X DELETE "repos/$REPO/issues/comments/$id" || (($? == 2))
  done
}

IFS=' ' read -ra tokens <<<"${EVICT_PRS:-}"
stuck=""
for token in "${tokens[@]}"; do
  num="${token#\#}"
  # The caller builds this list, so a token that is not a number means the two
  # sides disagree on the format — say so rather than sending it to the API.
  if [[ ! "$num" =~ ^[0-9]+$ ]]; then
    echo "::warning::ignoring '$token': EVICT_PRS carries PR numbers, optionally '#'-prefixed."
    continue
  fi
  rc=0
  pr_merge_queue_state "$REPO" "$num" || rc=$?
  ((rc == 0)) || continue
  if pr_dequeue_merge_queue_entry "$REPO" "$num"; then
    echo "::notice::evicted PR #${num}'s merge-queue entry — the PR conflicts with its base, so the queue can never build it."
    clear_stuck_notice "$num"
  else
    stuck="$stuck #$num"
    stuck_notice "$num"
  fi
done

if [[ -n "$stuck" ]]; then
  echo "::warning::merge-queue entries left in place for$stuck — this run could not drop them; remove them in the queue UI."
fi
