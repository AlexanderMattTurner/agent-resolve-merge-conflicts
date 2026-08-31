#!/usr/bin/env bash
# Approve a pull request the Claude reviewer deliberately SKIPS — a bot-authored
# PR, or a trusted author's chore/style/release title. The reviewer's own APPROVE
# vote is what satisfies a review-required ruleset for the PRs it reads, so a
# class it never reads carries no approving review and waits for a person. This
# posts that review. The caller (claude-review.yaml's auto-approve-skipped job)
# has already decided the PR is in the skip set.
#
# Requires: GH_TOKEN, GH_REPO, PR.
set -euo pipefail

: "${PR:?PR number required}"
: "${GH_REPO:?GH_REPO required}"

gh pr review "$PR" --approve --body \
  "Automated approval: the Claude reviewer skips this PR class (bot-authored, or a chore/style/release title), so nothing else supplies the review a review-required ruleset asks for. Add the \`needs-auto-review\` label to have Claude review it anyway."
