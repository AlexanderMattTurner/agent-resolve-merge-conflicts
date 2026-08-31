#!/usr/bin/env bash
# Approve a pull request the Claude reviewer deliberately SKIPS — a bot-authored
# PR, or a trusted author's chore/style/release title. The reviewer's own APPROVE
# vote is what satisfies a review-required ruleset for the PRs it reads, so a
# class it never reads carries no approving review and waits for a person. This
# posts that review. The caller (claude-review.yaml's auto-approve-skipped job)
# has already decided the PR is in the skip set.
#
# The approval carries NO BODY, and that is load-bearing. pr-reviews.bash's
# reviewer_reviews_ndjson selects every github-actions review with a non-empty
# body, whatever its state, and two consumers read it: review_findings_gate.py's
# "has been reviewed at all" term, and the reviewer's own owed-review decision. A
# bodied approval would race note-skipped-review on the same event and could
# suppress its note, then spend the one-review budget a later needs-auto-review
# label needs. note-skipped-review already carries the explanation and the label
# offer, so the body would be a duplicate of it.
#
# Idempotent, so the caller can run it on `synchronize` as well as the first
# look: a PR already carrying this approval, or one whose approval a reviewer
# dismissed, exits 0 without posting again.
#
# Requires: GH_TOKEN, GH_REPO, PR.
set -euo pipefail

: "${PR:?PR number required}"
: "${GH_REPO:?GH_REPO required}"

# APPROVED means one is already on record; DISMISSED means a reviewer took it
# down on purpose, and re-posting would overturn that. The paging REST endpoint,
# not `gh pr view --json reviews`: that reads a connection gh caps at 100 with no
# cursor, so a long-running PR's early approval would fall off the list and this
# would approve a second time. The jq filter emits one line per review and
# reduces nothing, so running it per page is correct.
states="$(gh api --paginate "repos/${GH_REPO}/pulls/${PR}/reviews" \
  --jq '.[] | select(.user.login == "github-actions[bot]") | .state')"
case $'\n'"$states"$'\n' in
*$'\n'APPROVED$'\n'* | *$'\n'DISMISSED$'\n'*)
  echo "auto-approve-skipped: PR #${PR} already carries a github-actions approval or dismissal; nothing to post."
  exit 0
  ;;
*) ;; # no approval and no dismissal on record — post one below
esac

gh pr review "$PR" --repo "$GH_REPO" --approve
