#!/usr/bin/env bash
# Sticky-upsert the remerge-diff report as a PR comment: PATCH the existing
# marked comment when present, else POST one. When the report is empty (no
# hand-authored resolution deltas remain on the current head), an existing
# comment from an earlier push is PATCHed to say so — deleting it would erase
# the record that a delta was ever flagged, and leaving it stale would claim a
# delta that no longer exists.
#
# The sticky marker is the renderer's own constant, read from the trusted
# checked-out module — not from the report body — so PR-controlled diff
# content cannot widen the sticky-comment match. Env: GH_TOKEN, REPO,
# PR_NUMBER, REPORT_FILE, and GH_TOKEN_READ (optional; a second credential the
# two reads spend, on a rate-limit budget separate from GH_TOKEN's).
set -euo pipefail

# The merge-delta review (post-merge-delta-review.sh) folds its findings
# into this same comment as a delimited block; preserve it across a delta
# refresh so re-rendering the deltas does not wipe the review. Its markers and
# the sticky marker both come from the resolver clone this job pinned, so this
# preserver and that writer read ONE definition.
: "${RESOLVER_DIR:?RESOLVER_DIR required — the resolver clone holds the renderer}"
# shellcheck source=.github/resolver/lib/merge-delta-verdict.bash
source "${RESOLVER_DIR}/lib/merge-delta-verdict.bash"
# shellcheck source=.github/resolver/lib-ci-retry.sh
source "${RESOLVER_DIR}/lib-ci-retry.sh"
# shellcheck source=.github/resolver/lib-marker-comment.sh
source "${RESOLVER_DIR}/lib-marker-comment.sh"
marker="$(delta_marker)"

# report_render renders with ITS checkout's renderer, which on a PR that
# changes MARKER is not the copy this job trusts. Post under the trusted
# marker, so the search that finds this comment on the next push cannot miss it
# and post a duplicate every time.
if [[ -s "$REPORT_FILE" ]]; then
  IFS= read -r first <"$REPORT_FILE" || first=""
  if [[ "$first" != "$marker" ]]; then
    normalized="$(mktemp)"
    {
      printf '%s\n' "$marker"
      tail -n +2 "$REPORT_FILE"
    } >"$normalized"
    mv "$normalized" "$REPORT_FILE"
  fi
fi

# The two READS below spend GH_TOKEN_READ where a caller supplies one, then
# every write reverts to GH_TOKEN. A push starts this repository's whole check
# fan-out on one 1000-an-hour budget, so a caller with a second credential
# keeps these reads off the budget its own writes need.
write_token="$GH_TOKEN"
GH_TOKEN="${GH_TOKEN_READ:-$GH_TOKEN}"
# A failed listing must stay distinguishable from "no existing comment" —
# masking both as empty would POST a duplicate every run, so a non-zero return
# here aborts under `set -e` rather than falling through to the POST.
# shellcheck disable=SC2153 # REPO is the workflow step's env var, not a typo of the lib's `repo`.
existing="$(marker_owned_comment_id "repos/$REPO/issues/$PR_NUMBER/comments" "$marker")"
GH_TOKEN="$write_token"

if [[ ! -s "$REPORT_FILE" ]]; then
  [[ -n "$existing" ]] || exit 0
  printf '%s\n%s\n' "$marker" \
    "## Hand-authored merge-resolution deltas: none on the current head." \
    >"$REPORT_FILE"
fi

if [[ -n "$existing" ]]; then
  # Carry any folded review block (start..end inclusive) onto the fresh report.
  # The body read here is also what the write below compares against, so the
  # no-op guard costs no second round trip.
  body_tmp="$(mktemp)"
  review_tmp="$(mktemp)"
  GH_TOKEN="${GH_TOKEN_READ:-$GH_TOKEN}"
  retry_stdout gh api "repos/$REPO/issues/comments/$existing" --jq .body >"$body_tmp"
  GH_TOKEN="$write_token"
  awk -v s="$REVIEW_START" -v e="$REVIEW_END" '
    index($0, s) == 1 { inb = 1 }
    inb { print }
    index($0, e) == 1 { inb = 0 }
  ' "$body_tmp" >"$review_tmp"
  if [[ -s "$review_tmp" ]]; then
    printf '\n' >>"$REPORT_FILE"
    cat "$review_tmp" >>"$REPORT_FILE"
  fi
  patch_comment_if_changed "repos/$REPO/issues/comments/$existing" "$REPORT_FILE" "$body_tmp"
  rm -f "$body_tmp" "$review_tmp"
else
  retry_stdout gh api -X POST "repos/$REPO/issues/$PR_NUMBER/comments" -F body=@"$REPORT_FILE" >/dev/null
fi
