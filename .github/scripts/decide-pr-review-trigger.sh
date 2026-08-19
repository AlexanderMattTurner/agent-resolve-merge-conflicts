#!/usr/bin/env bash
# Decide whether the PR reviewer (claude-review.yaml's `review` job) should run
# for this pull_request_target event, emitting run=true/false AND the model to
# use to GITHUB_OUTPUT.
#
# BUDGET — ONE whole-diff read per pull request. A later push is not re-read:
# the reviewer's findings live on review threads, and resolving an addressed
# thread is the session's own job, not another paid Opus pass per push. The
# review-findings gate holds the merge on those threads, so it clears on a
# resolution rather than on a re-review.
#
#   opened — always review: the one whole-diff read, and the only unconditional
#     arm, because GitHub fires it exactly once per pull request.
#   labeled — review on demand, when the "needs-auto-review" label is applied.
#     The escape hatch the skipped-review note points at: a PR the reviewer
#     skipped by title/author (chore/style, or a bot) gets a real read when a
#     human adds the label. Any other label is a no-op (run=false).
#   ready_for_review / synchronize — both repeat without limit, so they review
#     only when one of two conditions holds:
#       1. "[opus-review]" in the head commit TITLE (a push only) — a full,
#          on-demand re-read. Head-scoped: one tagged commit buys one read, and
#          a later untagged push buys none (re-tag to run again).
#       2. The reviewer left NO review of this pull request at all — re-arming
#          the read `opened` owed but never delivered, after a cancelled job or
#          an oversized diff. Self-terminating: the first review ends it. Any
#          review STATE spends the read, a dismissal included: dismissing a
#          COMMENT review moves no merge lever now that the gate is a status
#          check, so it is not a request for another paid pass.
#
# Read under pull_request_target, so the untrusted PR head is NEVER checked out
# or executed here: the head commit's message and the PR's reviews are fetched as
# DATA via the API and matched as FIXED strings (grep -F / exact compare, never
# eval). A transient API failure yields run=false (no review, no red) rather than
# a spurious re-review.
#
# Env: GH_TOKEN, ACTION, REPO, HEAD_SHA, PR, LABEL (LABEL set only on `labeled`).
set -euo pipefail

REPO="${REPO:?REPO (owner/name) required}"
PR="${PR:?PR number required}"
owner="${REPO%%/*}"
name="${REPO##*/}"
KEYWORD="[opus-review]"
REVIEW_LABEL="needs-auto-review"
# The reviewer posts with GITHUB_TOKEN, so its reviews are authored by this bot;
# ANY review from it means the one whole-diff read is already spent.
REVIEWER="github-actions[bot]"
export REVIEWER_LOGIN_BARE="${REVIEWER%'[bot]'}" # bare, since GraphQL omits the REST `[bot]` suffix
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The ONE definition of "what has the reviewer posted on this PR?", shared with
# review_findings_gate.py's reviewed-at-all term: a second copy would drift, and a
# trigger that says "already reviewed" while the gate says `pending` strands the
# pull request.
# shellcheck source=.github/resolver/lib/pr-reviews.bash disable=SC1091
source "$(cd "$SCRIPT_DIR/../resolver/lib" && pwd)/pr-reviews.bash"
# The one model behind every read that decides whether this PR's findings gate
# holds. Not a place to economize.
REVIEW_MODEL="claude-opus-5"

emit() {
  # $1 run, $2 reason
  local run="$1" reason="$2"
  {
    echo "run=$run"
    echo "model=$REVIEW_MODEL"
  } >>"$GITHUB_OUTPUT"
  echo "decision: run=$run model=$REVIEW_MODEL ($reason)"
}

case "$ACTION" in
opened)
  emit true "first review on $ACTION"
  exit 0
  ;;
labeled)
  if [[ "${LABEL:-}" == "$REVIEW_LABEL" ]]; then
    emit true "on-demand review requested via '$REVIEW_LABEL' label"
  else
    emit false "labeled with '${LABEL:-}', not '$REVIEW_LABEL'"
  fi
  exit 0
  ;;
ready_for_review | synchronize) ;;
*)
  emit false "no automatic review on '$ACTION'"
  exit 0
  ;;
esac

# Trigger 1: full Opus re-read on the [opus-review] opt-in in the head commit
# title, on a PUSH alone — the push carries the tagged head, so one tagged commit
# buys exactly one read, where a `ready_for_review` toggle carrying no new commit
# would buy one per toggle off a single head. Fetch the head commit DIRECTLY by
# SHA — not the PR-commits list, which the API caps at 250 even with --paginate,
# so on a heavily-revised PR (exactly what this re-trigger serves) the head would
# fall off the list and the opt-in would silently fail. Capture into a variable
# (never `gh … | grep`, whose early-exit SIGPIPEs the still-writing gh under
# pipefail), then match the subject line.
if [[ "$ACTION" == "synchronize" ]]; then
  # allow-exit-suppress: a failed API read must degrade to "no [opus-review] tag" — this is the fail-safe direction the header above documents (transient failure -> run=false, never a spurious re-review).
  message="$(gh api "repos/$REPO/commits/$HEAD_SHA" --jq '.commit.message' 2>/dev/null)" || message=""
  subject="${message%%$'\n'*}"
  if grep -qiF "$KEYWORD" <<<"$subject"; then
    emit true "$KEYWORD in head commit title"
    exit 0
  fi
fi

# Trigger 2: the reviewer never reviewed this pull request, so the one read
# `opened` owed was never delivered — a cancelled job, an oversized diff, a
# failed run.
#
# The exit STATUS is captured separately from the state, because the two empty
# results mean opposite things: a successful "" is the strongest reason to review
# (nobody ever looked), while a failed "" must keep the fail-safe of not
# reviewing. Folded together they would review on every API blip.
reviews_rc=0
latest="$(latest_reviewer_review "$owner" "$name" "$PR" 2>/dev/null)" || reviews_rc=$?
state="$(jq -r '.state // ""' <<<"$latest")"
if [[ "$reviews_rc" -ne 0 ]]; then
  emit false "could not read this PR's reviews (rc=$reviews_rc); leaving the review budget as it stands"
elif [[ -z "$state" ]]; then
  emit true "$REVIEWER has not reviewed this PR yet — re-arming the owed first read"
else
  emit false "the one whole-diff read is spent (latest: $state) and no $KEYWORD opt-in is on the head"
fi
