#!/usr/bin/env bash
# PROBLEM CLASS: `gh api POST .../reviews` can be rejected under GITHUB_TOKEN when
# the repo does not allow Actions to post a review at all. Every script here that
# posts a PR review through the reviews API goes through this helper, so a rejected
# call still delivers the review's text as a plain PR comment instead of vanishing.
#
# post_review_with_retry <pr> <payload_json_path> <fallback_comment_path>
# payload_json_path is a JSON object with at least {event, body}, ready for
# `gh api --input`. fallback_comment_path is posted as a plain PR comment only if
# the reviews API rejects the review.
post_review_with_retry() {
  local pr="$1" payload="$2" fallback="$3"

  if gh api -X POST "repos/${GH_REPO}/pulls/${pr}/reviews" --input "$payload" >/dev/null; then
    echo "posted review" >&2
    return 0
  fi

  echo "::warning::reviews API rejected the review; posting a summary comment instead" >&2
  gh pr comment "$pr" --body-file "$fallback"
}
