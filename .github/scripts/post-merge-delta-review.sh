#!/usr/bin/env bash
# Fold the merge-delta reviewer's findings INTO the remerge-diff
# supervision comment (the "Hand-authored merge-resolution deltas" sticky) so a
# reviewer reads the deltas and their review in ONE comment instead of two. The
# review is a delimited block ($REVIEW_START..$REVIEW_END) appended to that
# comment; the remerge-diff renderer (remerge-diff-comment.sh) preserves the
# block when it refreshes the deltas, so the two workflows cooperate on one
# comment without clobbering each other.
#
# The model's text is derived from the untrusted merge delta, so it is run
# through the SAME agent-input-sanitizer as the input before it reaches a posted
# comment — a hidden payload the model echoed from the delta can't ride into the
# comment.
#
# Advisory only: this posts/patches a comment, never a REQUEST_CHANGES review, so
# a finding never hard-blocks the merge (a human decides).
#
# Runs on every push where the prepare step SUCCEEDED (not only when there were
# deltas), so the review block stays truthful across transitions. HAD_DELTAS
# comes from the RENDERER, and only it separates the two absent-file cases:
#   - merge-review.md present → the model's findings (or its clean verdict);
#   - absent, HAD_DELTAS=false → the head really has no hand-authored merge
#     deltas, so a concern about a since-removed merge stops showing stale;
#   - absent, HAD_DELTAS=true  → deltas exist and the reviewer produced nothing
#     (it errored, or ran and wrote no file). UNREVIEWED, and a concern. This
#     step runs even when the reviewer step above it went red, so inferring
#     HAD_DELTAS from the file would publish that state as clean.
#
# Fallback: when the remerge-diff comment is absent — a fork PR (whose remerge
# comment step is skipped for lack of a write token) or a rare race where the
# review finishes before the deltas are posted — the review lives on its OWN
# sticky comment so the findings are never lost. A concern creates that
# fallback; a clean verdict only updates an existing one.
#
# Requires: GH_TOKEN, GH_REPO, PR, PR_INPUT_DIR, RESOLVER_DIR; node with the
# sanitizer on the module path.
set -euo pipefail

# From the resolver clone this job PINNED, never a repo-relative path: the
# renderer that wrote the sticky comment is the pinned one, so a marker read
# from a tree copy on some other sha would match no comment and post a second.
: "${RESOLVER_DIR:?RESOLVER_DIR required — the resolver clone holds the renderer}"
# shellcheck source=.github/resolver/lib/merge-delta-verdict.bash
source "${RESOLVER_DIR}/lib/merge-delta-verdict.bash"

: "${PR:?PR number required}"
: "${GH_REPO:?GH_REPO required}"
: "${PR_INPUT_DIR:?PR_INPUT_DIR required}"

DELTA_MARKER="$(delta_marker)"
review="${PR_INPUT_DIR}/merge-review.md"

# HAD_DELTAS comes from the RENDERER, never from whether the reviewer wrote a
# file. Inferring it from `[[ -s "$review" ]]` reads "the model produced
# nothing" as "there was nothing to review", and then publishes that as a clean
# verdict on the one state that must fail closed: real deltas, no review behind
# them. A model turn that exits 0 having written no file is exactly that state.
: "${HAD_DELTAS:?HAD_DELTAS required — pass the renderer has_deltas output}"
# The renderer emits exactly two values, and every test below is `== "true"`, so
# anything else — `True`, `1`, a renaming of the output — would fall through to
# the no-deltas branch and publish an unreviewed head as clean. That is the
# fail-open this change removes, so reject the unknown value instead.
[[ "$HAD_DELTAS" == "true" || "$HAD_DELTAS" == "false" ]] || {
  echo "post-merge-delta-review: HAD_DELTAS must be true or false, got '${HAD_DELTAS}'" >&2
  exit 1
}

# Deltas exist but the reviewer left nothing: UNREVIEWED, not clean.
reviewed=true
if [[ "$HAD_DELTAS" == "true" && ! -s "$review" ]]; then
  reviewed=false
fi

# Whether a real VERDICT reached the PR — what the gate's MERGE_DELTA_VERDICT_IN_HAND exemption is about, and NOT what exiting 0 claims. The UNREVIEWED branch posts successfully and judges nothing, so a gate keyed on this step's outcome alone would skip its merge-delta term and publish green over a head no reviewer read. `HAD_DELTAS=false` IS a verdict, so only the unreviewed case withholds it.
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  printf 'verdict_in_hand=%s\n' "$reviewed" >>"$GITHUB_OUTPUT"
fi

# The review BLOCK, delimited and sanitized. This is spliced into the
# remerge-diff comment, or posted standalone in the fallback.
block="$(mktemp)"
{
  printf '%s\n' "$REVIEW_START"
  printf '## Merge-resolution review\n\n'
  if [[ "$HAD_DELTAS" == "true" && "$reviewed" == "true" ]]; then
    # Sanitize the model output before it reaches the comment.
    node "${RESOLVER_SCRIPTS:?RESOLVER_SCRIPTS is unset — the sanitizer must come from the pinned tree, never the working directory}/sanitize-pr-input.mjs" <"$review"
  elif [[ "$HAD_DELTAS" == "true" ]]; then
    printf 'UNREVIEWED — this head carries merge-resolution deltas and the reviewer produced no verdict. Read the deltas above by hand.\n'
  else
    printf 'No merge-resolution deltas on the current head.\n'
  fi
  printf '\n<sub>Advisory review of this PR'\''s hand-authored merge-resolution deltas (git show --remerge-diff) — the one channel an evil merge can hide in. Non-blocking.</sub>\n'
  printf '%s\n' "$REVIEW_END"
} >"$block"

# Only a *findings* body warrants CREATING the standalone fallback; a clean
# verdict (model found nothing, or there are no deltas) only ever UPDATES an
# existing one — so a fork PR that never had a concern stays silent. The verdict
# is read through the shared predicate, so a review that merely mentions or
# quotes the all-clear among findings is a concern here, exactly as it is to the
# resolver's self-review.
# An UNREVIEWED head is a concern: it is the state a silent model produces, and
# staying quiet there is what would publish it as clean.
is_concern=false
if [[ "$HAD_DELTAS" == "true" ]] && { [[ "$reviewed" != "true" ]] || ! review_is_clean "$review"; }; then
  is_concern=true
fi

# Capture each listing on its own line so an auth/list failure is
# distinguishable from "no such comment" — masking both would double-post.
delta_list="$(gh api --paginate "repos/${GH_REPO}/issues/${PR}/comments" \
  --jq ".[] | select(.body | startswith(\"$DELTA_MARKER\")) | .id")"
delta_id="${delta_list%%$'\n'*}"

# Drop any existing review block (start..end inclusive) from stdin. index()==1
# tolerates a trailing CR on the marker line.
strip_review_block() {
  awk -v s="$REVIEW_START" -v e="$REVIEW_END" '
    index($0, s) == 1 { inb = 1 }
    !inb { print }
    index($0, e) == 1 { inb = 0 }
  '
}

if [[ -n "$delta_id" ]]; then
  # Fold: splice the fresh review block onto the remerge-diff comment, replacing
  # any prior block. A single blank line separates the deltas from the review;
  # $(cat) trims trailing blanks so repeated refreshes never accumulate them.
  stripped="$(mktemp)"
  merged="$(mktemp)"
  gh api "repos/${GH_REPO}/issues/comments/${delta_id}" --jq .body |
    strip_review_block >"$stripped"
  {
    printf '%s\n\n' "$(cat "$stripped")"
    cat "$block"
  } >"$merged"
  gh api -X PATCH "repos/${GH_REPO}/issues/comments/${delta_id}" -F body=@"$merged" >/dev/null
  rm -f "$stripped" "$merged"

  # Clean up any orphan standalone review sticky left by a pre-fold run so the
  # review shows in exactly one place.
  orphans="$(gh api --paginate "repos/${GH_REPO}/issues/${PR}/comments" \
    --jq ".[] | select(.body | startswith(\"$REVIEW_START\")) | .id")"
  while IFS= read -r orphan; do
    [[ -n "$orphan" ]] || continue
    gh api -X DELETE "repos/${GH_REPO}/issues/comments/${orphan}" >/dev/null
  done <<<"$orphans"
  rm -f "$block"
  exit 0
fi

# Fallback: no remerge-diff comment (fork PR / race). Keep the review on its own
# sticky so the findings are never lost.
review_list="$(gh api --paginate "repos/${GH_REPO}/issues/${PR}/comments" \
  --jq ".[] | select(.body | startswith(\"$REVIEW_START\")) | .id")"
existing="${review_list%%$'\n'*}"

if [[ -n "$existing" ]]; then
  gh api -X PATCH "repos/${GH_REPO}/issues/comments/${existing}" -F body=@"$block" >/dev/null
elif [[ "$is_concern" == "true" ]]; then
  gh api -X POST "repos/${GH_REPO}/issues/${PR}/comments" -F body=@"$block" >/dev/null
fi
rm -f "$block"
