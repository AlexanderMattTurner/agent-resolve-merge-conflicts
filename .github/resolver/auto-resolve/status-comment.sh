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
#   run_failed  — a job died, was cancelled or timed out with no verdict published
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
# own body (STATE=verdict), its own reason (STATE=refused) or a run that died before it
# read a PR field (STATE=run_failed) holds no BASE_REF, and dying on a variable it never
# reads would drop the diagnosis it came here to publish.
if [[ "$STATE" != verdict && "$STATE" != refused && "$STATE" != run_failed ]]; then
  : "${BASE_REF:?BASE_REF required}"
fi

# One definition of this link, in lib/run-url.bash: the commit-status marks that
# outlive this comment carry the same URL.
run_link="$(pr_status_comment_run_link)"

# The step whose failure ended the run, for the one ending that otherwise names
# nothing. A PROVISIONING failure never reaches the model, so no refusal comment
# describes it, and the per-step debug report that would is behind an input somebody
# had to set BEFORE the run — which nobody does until it has already happened twice.
# Silent on an unreadable API: the generic sentence is worse than a name, and better
# than a wrong one.
# The status is read on its own line, never through a pipe: `gh api` prints the HTTP
# ERROR BODY to stdout, so piping straight into `head` names that body as the step —
# `…/jobs"}` reached a test comment that way. A failed read must yield nothing.
_gave_up_reason() {
  local step listing
  listing="$(gh api "repos/${GH_REPO:-${GITHUB_REPOSITORY:-}}/actions/runs/${GITHUB_RUN_ID:-}/jobs" \
    --paginate --jq '.jobs[].steps[]? | select(.conclusion == "failure") | .name' 2>/dev/null)" || listing=""
  # The first line, by parameter expansion rather than `| head -n 1`: head exits as
  # soon as it has its line, and that early exit trips the caller's pipefail.
  step="${listing%%$'\n'*}"
  if [[ -n "$step" ]]; then
    # shellcheck disable=SC2016  # the backticks are markdown in the comment body, not a substitution
    printf 'It failed in its `%s` step;' "$step"
  else
    printf 'Read the run for the reason;'
  fi
}

case "$STATE" in
working)
  pr_status_comment_set "$PR" "🤖 **Auto-resolve is working on the merge conflict with \`${BASE_REF}\`** — ${run_link} has taken it on. This comment is rewritten with the result, so it always says where the attempt got to." working
  ;;
gave_up)
  # Assigned on its own line, never inlined in the argument: a substitution that runs
  # AS an argument has its exit status discarded, so a failure would reach the reader
  # as an empty phrase mid-sentence.
  gave_up_reason="$(_gave_up_reason)"
  pr_status_comment_finalize "$PR" "⚠️ **Auto-resolve gave up on the merge conflict with \`${BASE_REF}\`** — ${run_link} ended with no resolution, and nothing was pushed to this branch. The conflict is still there. ${gave_up_reason} a later push to either branch makes this PR eligible again."
  ;;
not_landed)
  pr_status_comment_finalize "$PR" "⚠️ **Auto-resolve stopped without pushing anything** — ${run_link} ended in its landing job, so the conflict with \`${BASE_REF}\` is still there and nothing on this branch changed. The next conflict scan retries."
  ;;
verdict)
  # A caller that already has its own diagnosis (bundle.py's refusal) publishes it as
  # THE verdict, so the run's one comment carries the reason rather than a second
  # comment carrying it beside a generic "gave up".
  : "${BODY:?BODY required when STATE=verdict}"
  # The link is appended HERE rather than by each caller, so every verdict reaches the
  # log it cites — the caller wrote its diagnosis before it knew which run publishes it.
  pr_status_comment_set "$PR" "${BODY}$(pr_status_comment_run_evidence)"
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
run_failed)
  # The ending every other state misses: a job that died, was cancelled or hit its
  # timeout BEFORE any step announced the run. `finalize` alone reaches nothing then,
  # because it rewrites an existing comment and there is none — so this posts one.
  # The pair is order-dependent: `set_if_absent` posts a body carrying no in-flight
  # marker, which the `finalize` below then leaves alone.
  : "${FAILED_JOBS:?FAILED_JOBS required when STATE=run_failed}"
  run_failed_body="⚠️ **Auto-resolve stopped without finishing** — ${run_link} ended in its ${FAILED_JOBS}, so this merge conflict is still there and nothing on this branch changed. The run holds the reason; a push to either branch makes this PR eligible again. Please report a repeat of this at https://github.com/${AUTO_RESOLVE_RESOLVER_REPO:-AlexanderMattTurner/agent-resolve-merge-conflicts}/issues."
  pr_status_comment_set_if_absent "$PR" "$run_failed_body"
  pr_status_comment_finalize "$PR" "$run_failed_body"
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
