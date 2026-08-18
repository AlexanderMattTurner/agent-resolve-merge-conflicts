# shellcheck shell=bash
# The mark an automated hold clearance leaves in its dismissal message, and the
# ONE definition both sides of that protocol read.
#
# PROBLEM CLASS — two scripts disagreeing about what a DISMISSED review means.
# approve-if-reviewer-hold-clear.sh dismisses the reviewer's CHANGES_REQUESTED to
# say "this hold is cleared", because GitHub bars an Actions token from approving.
# review-gate.sh reads the same dismissal as "no reviewer has spoken" and posts a
# `pending` status. Nothing then clears it: decide-pr-review-trigger.sh re-reviews
# a push only while the reviewer's latest state is CHANGES_REQUESTED or COMMENTED,
# so a dismissed hold leaves the gate pending with no event able to move it.
# Observed on agent-resolve-merge-conflicts#5.
#
# A dismissal a HUMAN wrote carries no mark, so it still returns the pull request
# to `pending` — which is what a human dismissing a review is asking for.
#
# Usage:
#   source "$SCRIPT_DIR/lib/reviewer-hold-mark.bash"
#   message="${reason} ${REVIEWER_HOLD_CLEARED_MARK}"      # the dismisser
#   … select(.dismissed_review.dismissal_message | contains(env.REVIEWER_HOLD_CLEARED_MARK))

[[ -n "${_REVIEWER_HOLD_MARK_SOURCED:-}" ]] && return 0
_REVIEWER_HOLD_MARK_SOURCED=1

# Exported because the jq filters that match it read `env.REVIEWER_HOLD_CLEARED_MARK`.
REVIEWER_HOLD_CLEARED_MARK="[automated hold clearance]"
export REVIEWER_HOLD_CLEARED_MARK
