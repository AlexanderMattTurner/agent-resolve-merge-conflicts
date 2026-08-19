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
#   # REVIEWER_SPOKEN_LOGIN empty -> the reviewer's latest review does not stand
#   # REVIEWER_SPOKEN_VIA_CLEARED_HOLD=1 -> it stands as an automated hold clearance

[[ -n "${_REVIEWER_SPOKEN_SOURCED:-}" ]] && return 0
_REVIEWER_SPOKEN_SOURCED=1

_REVIEWER_SPOKEN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/reviewer-login.bash disable=SC1091
source "$_REVIEWER_SPOKEN_DIR/reviewer-login.bash"
# shellcheck source=lib/reviewer-hold-mark.bash disable=SC1091
source "$_REVIEWER_SPOKEN_DIR/reviewer-hold-mark.bash"
reviewer_login_init

# $1 repo (owner/name), $2 PR number. Sets REVIEWER_SPOKEN_LOGIN to the
# reviewer's login when its LATEST review of this pull request still stands, and
# to "" when it does not. Returns 2 when the reviews read fails, so a caller
# decides for itself what an unreadable API means — never silently "not
# reviewed". The answer comes back in GLOBALS, not on stdout: a `$(…)` capture
# runs in a subshell, so the cleared-hold flag the caller needs would die with it.
# shellcheck disable=SC2034 # REVIEWER_SPOKEN_* are this function's return values, read by the caller
reviewer_spoken_login() {
  local repo="$1" pr="$2" reviews_json latest latest_state latest_login last_dismissal
  REVIEWER_SPOKEN_LOGIN=""
  REVIEWER_SPOKEN_VIA_CLEARED_HOLD=""

  # THE LATEST review decides, not any review: a second read leaves two reviews
  # on one PR, and a human who dismisses the newest is asking for another one.
  # An older undismissed review must not answer "already reviewed" over it.
  # `--paginate --slurp` is rejected together with `--jq`, so the pages are
  # captured raw; the array holds one element PER PAGE, hence the `.[][]` below.
  reviews_json="$(gh api --paginate --slurp "repos/${repo}/pulls/${pr}/reviews")" || return 2
  # Only the reviewer's OWN reviews count, and only ones carrying a body. Any
  # actor at all is self-clearing, since a PR author can green the required
  # context with a one-word review of their own PR. GitHub also synthesizes a
  # body-less COMMENTED review around a standalone review comment, and this repo
  # posts those under the reviewer's identity when something replies in-thread.
  latest="$(jq -r "[.[][] | ${REVIEWER_MATCH_USER} | select((.body // \"\") != \"\")]
                   | last | \"\(.state // \"\")\t\(.user.login // \"\")\"" <<<"$reviews_json")"
  latest_state="${latest%%$'\t'*}"
  latest_login="${latest#*$'\t'}"

  if [[ -z "$latest_state" ]]; then
    return 0 # the reviewer has never reviewed this pull request
  fi
  if [[ "$latest_state" != "DISMISSED" ]]; then
    REVIEWER_SPOKEN_LOGIN="$latest_login"
    return 0
  fi

  # A dismissed latest review counts only when THIS repository's automation
  # dismissed it: approve-if-reviewer-hold-clear.sh leaves a mark in the message
  # (lib/reviewer-hold-mark.bash) and a human's dismissal carries none. Only the
  # MOST RECENT dismissal is read, so one automated clearance cannot absorb every
  # later human one. Newlines are folded to keep each event on a single line.
  # allow-exit-suppress: a timeline this token cannot read is no evidence of a cleared hold, which leaves the answer where it already was — "not reviewed".
  last_dismissal="$(gh api --paginate "repos/${repo}/issues/${pr}/timeline" \
    --jq '.[] | select(.event == "review_dismissed")
          | ((.dismissed_review.dismissal_message) // "") | gsub("\r?\n"; " ")' |
    tail -n 1 || true)"
  if [[ "$last_dismissal" == *"$REVIEWER_HOLD_CLEARED_MARK"* ]]; then
    REVIEWER_SPOKEN_LOGIN="$latest_login"
    REVIEWER_SPOKEN_VIA_CLEARED_HOLD=1
  fi
  return 0
}
