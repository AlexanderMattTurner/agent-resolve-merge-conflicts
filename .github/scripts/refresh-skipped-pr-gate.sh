#!/usr/bin/env bash
# Re-fire the review-findings gate on the security rollup PR the scan just wrote.
#
# The scan's model step runs with GITHUB_TOKEN, whose events start no workflow, so
# the rollup PR it opens gets no pull_request_target run: no note clears the gate's
# reviewed-at-all leg and the PR stays blocked forever (#100). This step runs after
# the model, executes none of the scanned text, and holds the only user-actor
# token — so the `labeled` transition it writes is what those workflows hear.
#
# Inputs (env):
#   GH_TOKEN   A user-actor PAT. Empty disables the refresh, loudly.
#   GH_REPO    owner/repo, for `gh`.

set -euo pipefail

: "${GH_REPO:?GH_REPO must be set}"
RECHECK_LABEL=review-gate-recheck

if [[ -z "${GH_TOKEN:-}" ]]; then
  echo "::warning::AUTOFIX_TOKEN_ORG is not set, so the rollup PR keeps the events GITHUB_TOKEN raised — none. Its 'Review findings resolved' check never reports and the PR cannot merge. Set AUTOFIX_TOKEN_ORG (a user-actor PAT with pull-requests:write) to clear it automatically."
  exit 0
fi

gh label create "$RECHECK_LABEL" --force --color D93F0B \
  --description "One-shot: re-evaluate this PR's review-findings gate" >/dev/null

mapfile -t rollup_prs < <(
  gh pr list --state open --json number,headRefName \
    --jq '.[] | select(.headRefName | startswith("security-fixes/")) | .number'
)

if [[ ${#rollup_prs[@]} -eq 0 ]]; then
  echo "no open security-fixes/* pull request; nothing to refresh"
  exit 0
fi

for pr in "${rollup_prs[@]}"; do
  # `labeled` fires on a TRANSITION, so an add over a label already there fires
  # nothing. A 404 on the delete means the label was absent, which is the
  # ordinary case.
  gh api -X DELETE "repos/${GH_REPO}/issues/${pr}/labels/${RECHECK_LABEL}" >/dev/null 2>&1 || true
  gh api -X POST "repos/${GH_REPO}/issues/${pr}/labels" -f "labels[]=${RECHECK_LABEL}" >/dev/null
  echo "re-applied ${RECHECK_LABEL} to #${pr}"
done
