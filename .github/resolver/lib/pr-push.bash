# shellcheck shell=bash
# kcov-exclude: a GitHub Actions step body that no test runs: it reads the runner's context,
#   so it has no entry point off a runner.
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set shell options.
# The push half of a bot-authored write to a PR head branch, owned by auto-resolve/land.sh (the LLM-resolved merge commit). API:
#   git_as_bot GIT-ARGS… — run one git command under the bot identity, without writing that identity into the repo config.
#   git_auth_header TOKEN — authenticate this process's github.com git operations with TOKEN, through a transient http.extraheader in the GIT_CONFIG_* env. Re-exported from lib/git-auth.bash.
#   pick_push_token WORKFLOW-DELTA — choose the push token into PUSH_TOKEN. WORKFLOW-DELTA is the caller's own list of .github/workflows/ paths this push would change, empty when it changes none.
#   push_or_block REMOTE HEAD-REF PR-NUMBER BLOCKED-LABEL TOOL-NAME — push HEAD to HEAD-REF on REMOTE (a URL or a remote name — the pull request's HEAD repository, which is a fork for a cross-repository PR). 0 on success, $PUSH_BLOCKED (2) when the token lacks the `workflow` scope, $PUSH_NO_ACCESS (4) when it may not write REMOTE at all — both label the PR BLOCKED-LABEL — 1 otherwise.
#   push_retrying_races … — push_or_block plus non-ff recovery. Adds $PUSH_RACE_CONFLICT (3) when reconciling with the branch's new tip conflicts.
# `.claude/dev-notes` § "Bot push to a PR head branch (`.github/resolver/lib/pr-push.bash`)".

# shellcheck source=.github/resolver/lib-ci-retry.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib-ci-retry.sh"
# shellcheck source=.github/resolver/lib/pr-labels.bash
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pr-labels.bash"
# git_auth_header now lives in lib/git-auth.bash, which the non-pushing auth sites share.
# Sourced here rather than moved out of this API: every push script reaches the helper through
# this lib, and an unsourced helper is a 127 AFTER the work and BEFORE the push
# (tests/test_template_sync_resolve.py is that regression).
# shellcheck source=.github/resolver/lib/git-auth.bash
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/git-auth.bash"

# The identity every bot-authored commit carries.
BOT_NAME='github-actions[bot]'
BOT_EMAIL='41898282+github-actions[bot]@users.noreply.github.com'

# push_or_block's "this push will never succeed" status.
PUSH_BLOCKED=2
# push_retrying_races' "reconciling with the branch's new tip conflicts" status.
PUSH_RACE_CONFLICT=3
# push_or_block's "this token may not write the head repository at all" status.
PUSH_NO_ACCESS=4

# git_as_bot GIT-ARGS… — run one git command under the bot identity, without writing that
# identity into the repo config.
git_as_bot() {
  git -c "user.name=${BOT_NAME}" -c "user.email=${BOT_EMAIL}" "$@"
}

# pick_push_token WORKFLOW-DELTA — choose the push token, leave it in PUSH_TOKEN.
# TEMPLATE_SYNC_TOKEN_ORG carries the `workflow` scope AUTOFIX_TOKEN_ORG and GITHUB_TOKEN
# lack; the GITHUB_TOKEN rung fires no workflows (GitHub's recursion guard).
# shellcheck disable=SC2034 # PUSH_TOKEN is the output, read by the sourcing scripts
pick_push_token() {
  local workflow_delta="$1"
  if [[ -n "$workflow_delta" && -n "${TEMPLATE_SYNC_TOKEN_ORG:-}" ]]; then
    PUSH_TOKEN="$TEMPLATE_SYNC_TOKEN_ORG"
    echo "the push changes workflow files; pushing with TEMPLATE_SYNC_TOKEN_ORG (workflow-scoped):"
    printf '%s\n' "$workflow_delta"
  elif [[ -n "${AUTOFIX_TOKEN_ORG:-}" ]]; then
    PUSH_TOKEN="$AUTOFIX_TOKEN_ORG"
  else
    PUSH_TOKEN="$GITHUB_TOKEN"
    echo "WARNING: AUTOFIX_TOKEN_ORG is not set; pushing with GITHUB_TOKEN, which does NOT retrigger this PR's checks. The pushed head keeps the PR's existing (now stale) check results until a human-authored commit arrives — treat auto-merge with caution. Set AUTOFIX_TOKEN_ORG (a fine-grained PAT or GitHub App installation token with contents:write) to auto-revalidate." >&2
  fi
}

# push_or_block REMOTE HEAD-REF PR-NUMBER BLOCKED-LABEL TOOL-NAME — push and classify.
# Returns 0, $PUSH_BLOCKED for a `workflow`-scope refusal, $PUSH_NO_ACCESS when the token
# may not write REMOTE (both label BLOCKED-LABEL first), 1 otherwise.
# The push is NON-force, and the three `-c` overrides pin TLS verification on and
# clear both proxies, so the AUTHORIZATION header cannot be routed off-host by a git config file.
# --no-verify: a SessionStart hook may point git at `.hooks`, whose pre-push fails closed.
push_or_block() {
  local remote="$1" ref="$2" pr_num="$3" label="$4" tool="$5" push_out rc
  if push_out="$(git -c http.sslVerify=true -c http.proxy= -c "http.https://github.com/.proxy=" \
    push --no-verify "$remote" "HEAD:${ref}" 2>&1)"; then
    return 0
  fi
  printf '%s\n' "$push_out" >&2
  # Two PERMANENT refusals, and they need separate statuses: one is a missing
  # `workflow` scope on a token that can otherwise write the branch, the other is
  # a token with no write access to the head REPOSITORY — the shape a personal
  # fork answers when the secret is an org-scoped token that cannot reach it.
  # Anything else is transient, and the caller retries it.
  rc=1
  if grep -qE 'refusing to allow .* workflow' <<<"$push_out"; then
    rc="$PUSH_BLOCKED"
  elif grep -qE 'Write access to repository not granted|Permission to .* denied|[Rr]epository not found' <<<"$push_out"; then
    rc="$PUSH_NO_ACCESS"
  else
    return 1
  fi
  apply_blocked_label "$pr_num" "$label" "$tool"
  return "$rc"
}

# push_retrying_races REMOTE HEAD-REF PR-NUMBER BLOCKED-LABEL TOOL-NAME — push_or_block, plus
# recovery from a non-ff rejection: merge the branch's new tip (never force) and retry.
# Returns push_or_block's 0/$PUSH_BLOCKED/$PUSH_NO_ACCESS, $PUSH_RACE_CONFLICT on a merge
# conflict, else 1.
# Forcing would clobber the commits that won the race; an unresolved conflict ends the loop
# rather than pushing an auto-resolution.
push_retrying_races() {
  local remote="$1" ref="$2" pr_num="$3" label="$4" tool="$5"
  local attempt rc
  # retry-loop-ok: each attempt fetches and merges the branch's new tip before retrying — a race-reconciliation loop, not a blip retry lib-ci-retry.sh's single-command wrapper can express
  for attempt in 1 2 3; do
    rc=0
    push_or_block "$remote" "$ref" "$pr_num" "$label" "$tool" || rc=$?
    [[ "$rc" -eq 1 ]] || return "$rc"
    [[ "$attempt" -lt 3 ]] || return 1
    git fetch --no-tags --quiet "$remote" "$ref" || return 1
    git_as_bot merge --no-edit FETCH_HEAD || return "$PUSH_RACE_CONFLICT"
    sleep "$((attempt * 2))"
  done
}
