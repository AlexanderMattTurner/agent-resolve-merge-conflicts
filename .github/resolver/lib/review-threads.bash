# shellcheck shell=bash
# kcov-exclude: a GitHub Actions step body no test runs — it reads runner-only context or provisions the runner itself.
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set shell options.
# The PR review-thread read, in ONE place: the GraphQL document plus the
# `gh api graphql --paginate` call that walks it. INVARIANT: every step that needs a PR's review threads goes through fetch_review_threads, so no caller can ship a `reviewThreads(first: 100)` with no cursor — a query that silently drops every thread past the first page and reports the truncated slice as the whole set. Callers differ only in the jq they project each page's nodes through.
#
# Consumers: review_findings_gate.py.
#
# API:
#   fetch_review_threads <owner> <name> <pr> <jq> [comments-per-thread] — walk EVERY page, applying <jq> to each page's nodes ARRAY. Non-zero once retries exhaust.
#
# `.claude/dev-notes` § "PR review threads (`.github/resolver/lib/review-threads.bash`)".

# shellcheck source=.github/resolver/lib-ci-retry.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib-ci-retry.sh"

# `$endCursor` and `pageInfo` are what let `gh api graphql --paginate` walk: drop either and gh has no cursor to advance, so it returns page one forever and reports a truncated thread set as the whole one. That set feeds the review-findings merge gate, so an under-read greens a gate that should be red.
REVIEW_THREADS_QUERY=$(
  cat <<'GRAPHQL'
query($owner: String!, $name: String!, $pr: Int!, $comments: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(first: $comments) { nodes { fullDatabaseId author { login } body pullRequestReview { fullDatabaseId } } }
        }
      }
    }
  }
}
GRAPHQL
)

# jq predicate: thread's ROOT comment was authored by the reviewer (caller EXPORTs REVIEWER_LOGIN_BARE, both sides strip `[bot]`).
# shellcheck disable=SC2034 # spliced into the sourcing scripts' projections
REVIEW_THREAD_ROOT_IS_REVIEWER='select((.comments.nodes[0].author.login // "" | sub("\\[bot\\]$"; "")) == env.REVIEWER_LOGIN_BARE)'

# jq projection: review's fullDatabaseId as a STRING (review ids exceed Int32).
# shellcheck disable=SC2034 # spliced into the sourcing scripts' projections
REVIEW_THREAD_ROOT_REVIEW_ID='(.comments.nodes[0].pullRequestReview.fullDatabaseId // "" | tostring)'

# fetch_review_threads <owner> <name> <pr> <jq> [comments-per-thread] — walks every page, applying <jq> to each page's nodes ARRAY.
fetch_review_threads() {
  local owner="$1" name="$2" pr="$3" projection="$4" comments="${5:-1}"
  retry_stdout gh api graphql --paginate \
    -f query="$REVIEW_THREADS_QUERY" \
    -f owner="$owner" -f name="$name" -F pr="$pr" -F comments="$comments" \
    --jq ".data.repository.pullRequest.reviewThreads.nodes | $projection"
}
