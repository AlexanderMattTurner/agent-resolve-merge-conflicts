#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite: the suite runs the real
#   script as `bash <script>` against stubbed CLIs on PATH, so the branches are asserted but
#   no run is ever traced.
# Auto-resolve merge conflicts — STATUS-COMMENT step.
#
# Says on the pull request whether a resolver run is working on the conflict or has
# stopped, by keeping ONE comment current. The run posts it before it spends anything and
# rewrites it when it ends; the terminal steps (handoff.sh, land.sh) rewrite the same
# comment with their own verdict.
#
# Every state here is one a run reaches WITHOUT publishing a verdict, so each is the
# answer to "did the bot give up?" that the PR otherwise never gets:
#   working     — this run took the conflict on (posted before the merge and any model call)
#   gave_up     — the resolve job ended with no resolution to push
#   not_landed  — the landing job ended without pushing
#   no_op       — git merged the base cleanly, so there was nothing to resolve
#   refused     — discover declined the PR, so no resolve ever started
#
# Env: PR, BASE_REF, STATE, GH_TOKEN, GH_REPO, GITHUB_SERVER_URL, GITHUB_REPOSITORY,
# GITHUB_RUN_ID. STATE=refused adds REFUSED_RAIL and REFUSED_REASON.
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/resolver/lib-ci-retry.sh
source "$_SCRIPT_DIR/../lib-ci-retry.sh"
# shellcheck source=.github/resolver/lib/pr-status-comment.bash
source "$_SCRIPT_DIR/../lib/pr-status-comment.bash"

: "${PR:?PR required}"
: "${STATE:?STATE required}"
# Only the states whose own text names the branch demand it. A caller that brings its
# own body (STATE=verdict) or its own reason (STATE=refused) has no reason to hold
# BASE_REF, and dying on a variable it never reads would drop the diagnosis it came
# here to publish. A refusal also runs before any PR field is read, so the base ref
# is not in the environment at all.
if [[ "$STATE" != verdict && "$STATE" != refused ]]; then
  : "${BASE_REF:?BASE_REF required}"
fi

run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
run_link="[this run](${run_url})"

case "$STATE" in
working)
  pr_status_comment_set "$PR" "🤖 **Auto-resolve is working on the merge conflict with \`${BASE_REF}\`** — ${run_link} has taken it on. This comment is rewritten with the result, so it always says where the attempt got to." working
  ;;
gave_up)
  pr_status_comment_finalize "$PR" "⚠️ **Auto-resolve gave up on the merge conflict with \`${BASE_REF}\`** — ${run_link} ended with no resolution, and nothing was pushed to this branch. The conflict is still there. Read the run for the reason; a later push to either branch makes this PR eligible again."
  ;;
not_landed)
  pr_status_comment_finalize "$PR" "⚠️ **Auto-resolve stopped without pushing anything** — ${run_link} ended in its landing job, so the conflict with \`${BASE_REF}\` is still there and nothing on this branch changed. The next conflict scan retries."
  ;;
verdict)
  # A caller that already has its own diagnosis (bundle.py's refusal) publishes it as
  # THE verdict, so the run's one comment carries the reason rather than a second
  # comment carrying it beside a generic "gave up".
  : "${BODY:?BODY required when STATE=verdict}"
  pr_status_comment_set "$PR" "$BODY"
  ;;
refused)
  # The reason comes from discover, which is the one place each refusal is worded.
  # Named a filter, not by this tree's own word for it: the reader is the PR author.
  : "${REFUSED_RAIL:?REFUSED_RAIL required when STATE=refused}"
  : "${REFUSED_REASON:?REFUSED_REASON required when STATE=refused}"
  # set_if_absent, never set: this run did no work, so it must not overwrite the
  # verdict of a run that did. The queued duplicate the resolve job admits refuses
  # at the mark the first run wrote, and it reaches exactly this line.
  pr_status_comment_set_if_absent "$PR" "⚠️ **Auto-resolve is not resolving this merge conflict** — ${run_link} refused this pull request at its \`${REFUSED_RAIL}\` filter, before it spent anything. ${REFUSED_REASON}"
  ;;
no_op)
  # prepare reaches this exit on containment only — the base is already in the head, or
  # the head is already in the base. A clean merge that IS the resolution takes the
  # commit path instead, and land publishes its own body for it.
  pr_status_comment_finalize "$PR" "🤖 **Nothing to auto-resolve** — ${run_link} found no merge to make: one of this branch and \`${BASE_REF}\` already contains the other's commits, so nothing was pushed. Read the run for which side — a branch fully contained in \`${BASE_REF}\` carries nothing of its own."
  ;;
*)
  echo "status-comment.sh: unknown STATE '${STATE}'" >&2
  exit 2
  ;;
esac
