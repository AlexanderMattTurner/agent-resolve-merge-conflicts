#!/usr/bin/env bash
# Post a COMMENT review on a PR the Claude reviewer deliberately SKIPS (low-risk
# chore/style by title, a machine-cut `release:` PR, or a bot-authored PR).
#
# INVARIANT — the review-findings gate holds every PR until the reviewer has
# completed at least one review, so a class the reviewer never reads would sit at
# `pending` with no thread anyone could resolve. This review is what satisfies that
# leg. It carries NO merge vote: the gate is a status check, so an APPROVE here
# would only add a vote nothing asks for.
#
# The caller (claude-review.yaml's note-skipped-review job `if:`) has already
# decided this PR is in the skip set. The post goes through the shared helper
# (lib-post-review-with-retry.sh), which falls back to a plain PR comment when the
# reviews API rejects the call.
#
# Requires: gh authenticated (GH_TOKEN), GH_REPO, PR.
set -euo pipefail

# shellcheck source=.github/scripts/lib-post-review-with-retry.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib-post-review-with-retry.sh"

: "${PR:?PR number required}"
: "${GH_REPO:?GH_REPO required}"

# The label is offered only where it can WORK. claude-code-action refuses a
# Bot-initiated run, so telling a bot-authored PR to add the label sends it round
# the whole credential ladder for a refusal no token changes.
BODY="This PR type isn't Claude-reviewed (low-risk change or bot-authored), so this note stands in for the read and clears the review-findings gate's first leg. It raises no finding and casts no vote."
if [[ "${PR_AUTHOR_TYPE:-User}" != "Bot" ]]; then
  BODY="${BODY} Add the \`needs-auto-review\` label to have Claude review it anyway."
else
  BODY="${BODY} A bot-authored PR cannot be Claude-reviewed at all — the action refuses a non-human actor — so a human review is the only read this PR gets."
fi

payload="$(mktemp)"
fallback="$(mktemp)"
trap 'rm -f "$payload" "$fallback"' EXIT
jq -n --arg body "$BODY" '{event: "COMMENT", body: $body}' >"$payload"
printf '%s\n' "$BODY" >"$fallback"

post_review_with_retry "$PR" "$payload" "$fallback"
