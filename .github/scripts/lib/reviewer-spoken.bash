# shellcheck shell=bash
# Has the automated reviewer already reviewed this pull request?
#
# PROBLEM CLASS — two scripts disagreeing about whether a pull request has been
# reviewed. review-gate.sh posts the required "Automated review posted" status
# from that question, and decide-pr-review-trigger.sh spends the PR's one Opus
# read from it. A second copy of the predicate drifts, and the drift is silent:
# a gate that says `pending` while the trigger says "already reviewed" strands
# the pull request with no event able to move it.
#
# Usage:
#   source "$SCRIPT_DIR/lib/reviewer-spoken.bash"
#   reviewer_spoken_login "$GH_REPO" "$PR" || <the caller's read-failed path>
#   # REVIEWER_SPOKEN_LOGIN empty -> the reviewer has not reviewed this PR
#   # REVIEWER_SPOKEN_VIA_CLEARED_HOLD=1 -> the evidence is an automated hold clearance

[[ -n "${_REVIEWER_SPOKEN_SOURCED:-}" ]] && return 0
_REVIEWER_SPOKEN_SOURCED=1

_REVIEWER_SPOKEN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/reviewer-login.bash disable=SC1091
source "$_REVIEWER_SPOKEN_DIR/reviewer-login.bash"
# shellcheck source=lib/reviewer-hold-mark.bash disable=SC1091
source "$_REVIEWER_SPOKEN_DIR/reviewer-hold-mark.bash"
reviewer_login_init

# $1 repo (owner/name), $2 PR number. Sets REVIEWER_SPOKEN_LOGIN to the
# reviewer's login when it has reviewed this pull request, and to "" when it has
# not. Returns 2 when the reviews read fails, so a caller decides for itself what
# an unreadable API means — never silently "not reviewed". The answer comes back
# in GLOBALS, not on stdout: a `$(…)` capture runs in a subshell, so the
# cleared-hold flag the caller needs would die with it.
# shellcheck disable=SC2034 # REVIEWER_SPOKEN_* are this function's return values, read by the caller
reviewer_spoken_login() {
  local repo="$1" pr="$2" reviewers reviewer
  REVIEWER_SPOKEN_LOGIN=""
  REVIEWER_SPOKEN_VIA_CLEARED_HOLD=""

  # Every review that still stands, paginated: a long-lived PR accumulates more
  # than one page, and the filter is per-element because `--paginate --jq` runs
  # it on EACH page. A DISMISSED review is dropped, so dismissing the only review
  # returns the pull request to "not reviewed" — what a human asks for by
  # dismissing it. Two filters carry the rest:
  #   * BY THE REVIEWER — any actor at all is self-clearing, since a PR author
  #     can green the required context with a one-word review of their own PR.
  #   * WITH A BODY — GitHub synthesizes a body-less COMMENTED review around a
  #     standalone review comment, and this repo posts those under the reviewer's
  #     identity when something replies in-thread. Every REAL review here sends a
  #     body: post-pr-review.mjs falls back to "Automated review.", and
  #     auto-approve-skipped-pr.sh and approve-if-reviewer-hold-clear.sh hardcode
  #     theirs.
  reviewers="$(gh api --paginate "repos/${repo}/pulls/${pr}/reviews" \
    --jq ".[] | select(.state != \"DISMISSED\") | ${REVIEWER_MATCH_USER} | select((.body // \"\") != \"\") | .user.login // \"\"")" || return 2
  reviewer="$(head -n 1 <<<"$reviewers")"
  if [[ -n "$reviewer" ]]; then
    REVIEWER_SPOKEN_LOGIN="$reviewer"
    return 0
  fi

  # A hold this repository's own automation cleared still counts as a review.
  # approve-if-reviewer-hold-clear.sh dismisses the reviewer's CHANGES_REQUESTED
  # when GitHub refuses it an approval, and its mark is what says the dismissal
  # came from there rather than from a human (lib/reviewer-hold-mark.bash).
  # allow-exit-suppress: a timeline this token cannot read means no evidence of a cleared hold, which leaves the answer where it already was — "not reviewed".
  reviewer="$(gh api --paginate "repos/${repo}/issues/${pr}/timeline" \
    --jq "[.[] | select(.event == \"review_dismissed\")
          | select(((.dismissed_review.dismissal_message) // \"\")
                   | contains(env.REVIEWER_HOLD_CLEARED_MARK))] | length" |
    awk '{ total += $1 } END { if (total > 0) print ENVIRON["REVIEWER_LOGIN"] }' || true)"
  REVIEWER_SPOKEN_LOGIN="$reviewer"
  [[ -n "$reviewer" ]] && REVIEWER_SPOKEN_VIA_CLEARED_HOLD=1
  return 0
}
