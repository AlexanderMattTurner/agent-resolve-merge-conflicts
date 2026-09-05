#!/usr/bin/env bash
# Prepare the input for the merge-delta reviewer: fetch the PR head's
# commits as DATA (git objects only — never checked out, so no PR-authored code
# runs), render the --remerge-diff of the PR's OWN merge commits with the
# trusted base-checkout renderer, and sanitize it like any untrusted diff.
#
# The remerge-diff is the hand-authored delta of each merge resolution — the one
# place a conflict resolution can introduce content present in NEITHER parent (an
# "evil merge") that the ordinary PR diff never isolates. Emits has_deltas so the
# caller skips the model read entirely when the PR has no merges (or none with a
# hand-authored resolution).
#
# Requires: GH_TOKEN, GH_REPO, PR, PR_INPUT_DIR; a checkout with fetch-depth: 0
# and node + agent-input-sanitizer on the module path. BASE_REF names the branch
# the pull request merges into; unset asks the pull request for it.
# Emits to GITHUB_OUTPUT:
#   head_sha=<sha>             — the head this report describes, from
#                                refs/pull/N/head. Every step downstream names
#                                this one sha, so a verdict cannot describe one
#                                commit and land on another: the trigger
#                                payload's sha froze at dispatch, and this job
#                                can post half an hour later.
#   has_deltas=true|false      — whether there is a hand-authored merge delta
# Writes into $PR_INPUT_DIR:
#   merge-delta.shas.txt       — the sha of every merge rendered, always (empty
#                                when there are none). From the trusted renderer
#                                and never from a review, so a caller keying
#                                durable state on which merges an answer covered
#                                cannot have that set widened by the model.
# and only when has_deltas=true:
#   merge-delta.txt            — the sanitized remerge-diff report
#   merge-delta.report.txt     — what the sanitizer neutralized (if anything)
set -euo pipefail

: "${PR:?PR number required}"
: "${PR_INPUT_DIR:?PR_INPUT_DIR required}"
: "${GH_REPO:?GH_REPO required (to read the branch this pull request merges into)}"
# A workflow_dispatch payload carries no base ref, so ask the pull request for it.
if [[ -z "${BASE_REF:-}" ]]; then
  BASE_REF="$(gh pr view "$PR" --repo "$GH_REPO" --json baseRefName --jq .baseRefName)"
fi
# No apostrophe in this word: bash parses quotes inside ${var:?word}, so one there
# opens a string the rest of the file never closes.
: "${BASE_REF:?BASE_REF required (the base branch scopes the merges to this pull request)}"

mkdir -p "$PR_INPUT_DIR" # bare-mkdir-ok: post-condition verified on the next line
[[ -d "$PR_INPUT_DIR" ]] || {
  echo "PR_INPUT_DIR ($PR_INPUT_DIR) does not exist after mkdir -p" >&2
  exit 1
}

emit_output() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s\n' "$1" >>"$GITHUB_OUTPUT"
  fi
}

raw="$(mktemp)"
err="$(mktemp)"
trap 'rm -f "$raw" "$err"' EXIT

# Per-command auth only, so the checkout's persist-credentials:false stays
# intact. Fetches the OBJECTS of the PR head and of the branch it merges into.
# Neither is checked out — this is data for git to diff, not code to run. A
# failure is a can't-verify, so it fails loud rather than skipping the review.
auth="AUTHORIZATION: basic $(printf 'x-access-token:%s' "${GH_TOKEN:-}" | base64 | tr -d '\n')"
if ! timeout --kill-after=30 300 git -c "http.https://github.com/.extraheader=${auth}" \
  fetch --no-tags --quiet origin \
  "+refs/pull/${PR}/head:refs/remotes/pr/head" \
  "+refs/heads/${BASE_REF}:refs/remotes/pr/base"; then
  echo "::error::could not fetch refs/pull/${PR}/head and refs/heads/${BASE_REF} as data — cannot review this PR's merge deltas" >&2
  exit 1
fi
head_sha="$(git rev-parse refs/remotes/pr/head)"
emit_output "head_sha=$head_sha"
# The range starts at the branch this pull request merges INTO, so it holds the
# pull request's own commits and nothing else. Scoped to the DEFAULT branch, a
# stack layer's parent commits count as this pull request's, and each of the
# parent's merges draws a finding here that the parent already answered.
base_sha="$(git rev-parse refs/remotes/pr/base)"

# The renderer is deliberately fail-loud: it raises on a merge it cannot
# reconstruct, such as the octopus merge --remerge-diff refuses. Never swallow
# that. A non-zero exit reds this job, where masquerading as has_deltas=false
# would leave the reviewer quiet on exactly the merge that most needs eyes.
if ! BASE_SHA="$base_sha" HEAD_SHA="$head_sha" \
  python3 "${RESOLVER_DIR:?RESOLVER_DIR required — the resolver clone holds the renderer}/remerge-diff-report.py" \
  --shas-out "${PR_INPUT_DIR}/merge-delta.shas.txt" >"$raw" 2>"$err"; then
  echo "::error::the merge-delta renderer refused or failed — this PR's merges need a manual review, not a silent skip:" >&2
  cat "$err" >&2
  exit 1
fi

# rc 0 with empty output is the honest "no hand-authored deltas" case (no merge
# commits, or only clean mechanical merges) — that legitimately skips the review.
if [[ -s "$raw" ]]; then
  node "${RESOLVER_SCRIPTS:?RESOLVER_SCRIPTS is unset — the sanitizer must come from the pinned tree, never the working directory}/sanitize-pr-input.mjs" \
    <"$raw" >"${PR_INPUT_DIR}/merge-delta.txt" 2>"${PR_INPUT_DIR}/merge-delta.report.txt"
  emit_output "has_deltas=true"
else
  emit_output "has_deltas=false"
fi
