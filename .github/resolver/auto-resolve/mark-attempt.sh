#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body that no test runs: it reads the runner's context
#   — GITHUB_*, a job-scoped GH_TOKEN, an actions/ working directory — or provisions the
#   runner itself, so it has no entry point off a runner.
# Auto-resolve merge conflicts — MARK-ATTEMPT step.
#
# Records, on the head commit this job checked out, that the resolver ran against
# it. discover skips the PR while its head is unchanged and the base has not moved
# since the mark's timestamp, so a scan cannot re-run a paid resolution against the
# pair of trees that just produced one.
#
# Runs FIRST, before the merge and before any model call, so the mark survives a crash,
# a cancelled run and a timeout — marking on success only would leave exactly the
# expensive failures unmarked and free to repeat.
#
# It marks `git rev-parse HEAD` rather than the SHA discover emitted: if the branch moved
# between the scan and this checkout, THIS is the tree the run resolves. That same SHA
# goes on `$GITHUB_OUTPUT` as `head_sha`, since deriving it later reads a moved worktree.
#
# The mark is also this job's CLAIM on the head, because two scans of different SCOPES
# share no concurrency group and select the same head seconds apart. It settles before
# any spend: a read stands the job down on an earlier cycle's mark, then the write's own
# id arbitrates the runs that raced past that read (commit_status_mark_owns_claim
# carries the invariant), and `already_claimed=true` gates the expensive steps.
# Env: GH_TOKEN, REPO. Optional: AUTO_RESOLVE_ATTEMPT_TTL_HOURS (default 2, the same
# window discover holds a mark for), AUTO_RESOLVE_IGNORE_ATTEMPT_MARK=true to spend
# anyway — the catch-up input, which exists to re-run heads a mark is holding.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/resolver/lib-ci-retry.sh
source "$SCRIPT_DIR/../lib-ci-retry.sh"
# shellcheck source=.github/resolver/lib/auto-resolve-attempt.bash
source "$SCRIPT_DIR/../lib/auto-resolve-attempt.bash"

: "${REPO:?REPO required}"

emit() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "$1" >>"$GITHUB_OUTPUT"
  fi
}

head_sha="$(git rev-parse HEAD)"
ttl_hours="${AUTO_RESOLVE_ATTEMPT_TTL_HOURS:-2}"

# A claim that switches itself off on a typo is the wrong failure direction for a
# spend gate: a non-numeric value makes the window 0 seconds, so no mark is ever
# fresh and every scan buys the same head again.
if [[ ! "$ttl_hours" =~ ^[0-9]+$ ]] || ((ttl_hours < 1)); then
  echo "::error::AUTO_RESOLVE_ATTEMPT_TTL_HOURS must be a positive whole number of hours; got '${ttl_hours}'."
  exit 1
fi
ttl_secs="$((ttl_hours * 3600))"

# A fresh mark alone is no reason to stand down: discover re-enables a head once
# the mark passes its FLOOR and the base has moved, so a run it admitted used to
# stand down here on the TTL and resolve nothing. The mark's remaining job is to
# stop a CONCURRENT run spending on the same tree, so the question below is what
# the holding run is doing now. `unknown` still stands down, and reds the run.
if [[ "${AUTO_RESOLVE_IGNORE_ATTEMPT_MARK:-}" != "true" ]] &&
  commit_status_mark_fresh "$REPO" "$head_sha" "$AUTO_RESOLVE_ATTEMPT_CONTEXT" "$ttl_secs"; then
  case "$(auto_resolve_claim_state "$REPO" "$head_sha" "$ttl_secs")" in
  in_flight)
    echo "::notice::${head_sha}'s attempt mark is held by a run that is still going, so that run owns this head. Standing down before spending."
    emit "already_claimed=true"
    emit "claim=duplicate"
    exit 0
    ;;
  concluded)
    # Released only when the head cost nothing. A head that already bought a
    # resolution keeps its mark: discover re-enabled this run on its own floor
    # rule, and releasing here would also clear the mark for every other scan.
    if auto_resolve_head_bought "$REPO" "$head_sha"; then
      echo "::notice::${head_sha}'s attempt mark is held by a run that has concluded, and this head already bought a resolution. Taking it on again because discover selected it."
    else
      echo "::notice::${head_sha}'s attempt mark is held by a run that has concluded and bought nothing. Releasing the stale mark and taking this head on."
      auto_resolve_release_attempt "$REPO" "$head_sha" \
        "the run holding this attempt mark ended without releasing it, and it bought nothing"
    fi
    ;;
  *)
    echo "::error::${head_sha} carries an attempt mark naming no readable run, so this run cannot tell whether another one is spending on it. Standing down."
    emit "already_claimed=true"
    emit "claim=latched"
    exit 0
    ;;
  esac
fi

# Fails the step rather than proceeding unmarked. An unmarkable head is one every
# later scan re-buys at full model cost, so spending here would be unbounded — and
# the old best-effort write printed "Marked ..." either way, which made the loop
# invisible in the log.
run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-$REPO}/actions/runs/${GITHUB_RUN_ID:-0}"
if ! mark_id="$(auto_resolve_mark_attempt_strict "$REPO" "$head_sha" \
  "auto-resolve ran against this commit; a head push — or a base push once the mark ages past the floor — re-enables it" \
  "$run_url")"; then
  echo "::error::could not mark ${head_sha} as attempted; refusing to spend on a head no later scan would skip."
  exit 1
fi

# INVARIANT — this is what stops two runs paying for one tree; the read above cannot,
# because two runs that both reach it before either wrote both see an unmarked head.
# commit_status_mark_owns_claim carries the mechanism and the two fail directions. The
# mark this run wrote stays either way: the winner's own mark holds the head. Skipped
# on an unusable id, which keeps the whole claim's direction — unreadable is UNCLAIMED.
if [[ "${AUTO_RESOLVE_IGNORE_ATTEMPT_MARK:-}" != "true" ]] &&
  [[ "$mark_id" =~ ^[0-9]+$ ]] &&
  ! auto_resolve_owns_attempt "$REPO" "$head_sha" "$ttl_secs" "$mark_id"; then
  echo "::notice::an older attempt mark holds ${head_sha}, so the run that wrote it owns this head. Standing down before spending."
  emit "already_claimed=true"
  emit "claim=duplicate"
  exit 0
fi

# Written AFTER the mark, so the output names a mark that exists: a release step
# reading a SHA this script failed to mark would post a release for nothing.
emit "head_sha=${head_sha}"
emit "claim=owned"
echo "Marked ${head_sha} as attempted — later scans skip this PR until its head or base moves."
