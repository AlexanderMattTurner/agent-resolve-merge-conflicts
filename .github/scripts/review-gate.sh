#!/usr/bin/env bash
# Post the automated-review gate's verdict as a COMMIT STATUS on the PR head.
#
# PROBLEM CLASS — auto-merge landing a pull request before the reviewer has
# spoken. The cheap checks finish in about ninety seconds while an LLM review
# takes minutes, so a PR whose ruleset lists only the cheap checks merges first
# and the reviewer's REQUEST_CHANGES arrives on a merged PR. Nothing is red; the
# review simply was not part of the merge gate.
#
# The predicate is stateless: a pull request is clear when at least one
# undismissed review of it was written BY THE REVIEWER and carries a body. It
# needs no memory of which reviews have been seen, and it re-derives the same
# answer on every event. lib/reviewer-spoken.bash owns it, and carries why both
# halves of "by the reviewer, with a body" are load-bearing.
#
# PR-SCOPED, NOT HEAD-SCOPED, and that is load-bearing. Requiring a review OF THE
# CURRENT HEAD looks stricter and strands the pull request instead:
# decide-pr-review-trigger.sh answers run=false for a plain `synchronize`, so
# every push after the one whole-diff read produces a head nothing will ever
# review, and a head-scoped gate would hold that pull request at `pending`
# forever with no event able to clear it. Whether a later push still satisfies
# the reviewer is a question the reviewer's THREADS already own: the review-
# required ruleset holds the merge until each one is resolved. This gate answers
# only the question nothing else did — has the reviewer spoken about this pull
# request at all?
#
# A COMMIT STATUS, not this job's own check run. Under `pull_request_target` the
# job's check run is reported against the BASE commit, so it never satisfies a
# requirement evaluated on the pull request's head. A status posted explicitly on
# `HEAD_SHA` does.
#
# Can't-verify is RED, never green: an API failure propagates through `set -e`,
# because a gate that fails open lets a PR merge past a review nobody read.
#
# Env: GH_TOKEN, GH_REPO (owner/name), PR, HEAD_SHA, RUN_URL; REVIEWER_LOGIN
# optional.
set -euo pipefail

: "${GH_REPO:?GH_REPO required}"
: "${PR:?PR number required}"
: "${HEAD_SHA:?HEAD_SHA required}"
: "${GH_TOKEN:?GH_TOKEN required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/reviewer-spoken.bash disable=SC1091
source "$SCRIPT_DIR/lib/reviewer-spoken.bash"

# MUST stay byte-identical to the `name:` of the job in review-gate.yaml: that
# job name is what sync-required-checks registers as the ruleset's required
# context, and the status posted here has to carry the same context or the head
# never satisfies it.
GATE_CONTEXT="Automated review posted"

# The predicate — "has the reviewer reviewed this pull request?" — lives in
# lib/reviewer-spoken.bash, the ONE definition decide-pr-review-trigger.sh reads
# too. A failed reviews read exits non-zero here: a gate that fails open lets a
# PR merge past a review nobody read.
reviewer_spoken_login "$GH_REPO" "$PR"
reviewer="$REVIEWER_SPOKEN_LOGIN"
cleared_hold="$REVIEWER_SPOKEN_VIA_CLEARED_HOLD"

if [[ -n "$reviewer" ]]; then
  state=success
  if [[ -n "${cleared_hold:-}" ]]; then
    description="Reviewed by ${reviewer}; its hold was cleared automatically"
  else
    description="Reviewed by ${reviewer}"
  fi
else
  state=pending
  description="Waiting for the automated review of this pull request"
fi

# `pending`, not `failure`, for the not-yet-reviewed case: the review is coming,
# and a red would tell a reader to go diagnose something. Both hold the merge.
gh api -X POST "repos/${GH_REPO}/statuses/${HEAD_SHA}" \
  -f "state=${state}" \
  -f "context=${GATE_CONTEXT}" \
  -f "description=${description}" \
  -f "target_url=${RUN_URL:-}" >/dev/null

echo "posted ${state} status '${GATE_CONTEXT}' on ${HEAD_SHA}: ${description}" >&2
