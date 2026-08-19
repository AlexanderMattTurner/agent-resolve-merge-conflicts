#!/usr/bin/env bash
# Consume the one-shot review-gate-recheck label for review-findings-gate.yaml's evaluate job. Removing it is what keeps the label's semantics: a session that hand-resolves a review thread re-adds it to force a fresh evaluation, and `labeled` fires only on a transition, so a label left sitting on the PR makes the next re-add a no-op and the PR waits on a verdict nobody will post.
#
# A failure is RECORDED in GITHUB_ENV, never raised here: exiting non-zero would skip the verdict post that follows and leave the head's required check missing, which blocks the PR harder than a stuck label does.
#
# Reads: GH_REPO, PR. Writes: LABEL_REMOVAL_FAILED=1 to GITHUB_ENV on a real failure.
set -euo pipefail

: "${GH_REPO:?GH_REPO required}"
: "${PR:?PR number required}"

# pr-labels.bash sources the retry ladder itself, so this one line brings both.
# shellcheck source=.github/resolver/lib/pr-labels.bash
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../resolver/lib" && pwd)/pr-labels.bash"

# `gh api` exits 1 on every HTTP error, so a blanket retry around the DELETE cannot tell the 404 that means "no label to remove" from the 503 that means "ask again" — and on the common run there is no label, so retrying every failure would spend the whole backoff on a no-op. Read the label set first: that call retries with no ambiguity, and it makes the DELETE conditional, so the only failure left to report is a real one.
present=1 # a read that fails after its retries falls back to trying the DELETE
if names="$(retry_stdout gh pr view "$PR" --repo "$GH_REPO" --json labels --jq '.labels[].name')"; then
  case $'\n'"$names"$'\n' in
  *$'\n'"$PR_LABEL_REVIEW_GATE_RECHECK"$'\n'*) present=1 ;;
  *) present=0 ;; # the label is not on the PR: nothing to consume
  esac
fi
((present)) || exit 0

if ! out="$(retry_stdout gh api -X DELETE \
  "repos/${GH_REPO}/issues/${PR}/labels/${PR_LABEL_REVIEW_GATE_RECHECK}" 2>&1)"; then
  # gh renders the status as "(HTTP 404)"; the body text varies by endpoint, so match the code. Here it means another writer removed the label between the read above and this DELETE.
  case "$out" in
  *"(HTTP 404)"*) ;;
  *)
    echo "::error::${PR_LABEL_REVIEW_GATE_RECHECK} label removal failed — remove it manually so re-adding it can fire again: ${out}"
    # Defaulted because `set -u` would otherwise make an unset GITHUB_ENV exit this
    # script non-zero — the one outcome the header promises it never produces. Off a
    # runner there is no step to read the record, and the ::error:: above carries it.
    echo "LABEL_REMOVAL_FAILED=1" >>"${GITHUB_ENV:-/dev/null}"
    ;;
  esac
fi
