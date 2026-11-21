#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite: the suite runs the real
#   script as `bash <script>` against stubbed CLIs on PATH, so the branches are asserted but
#   no run is ever traced.
# Decide whether a claude-code-action attempt failed hard enough to retry on the
# fallback credential. The action exits 0 even on an is_error run, so the execution
# log is the only signal: errored=true on a missing/empty log (a crash) or is_error
# true; a genuine "nothing to do" run has is_error false and is NOT retried. This is
# the retry DECISION, not the gate (checks/claude-execution.py gates the final
# attempt). It fails the step in ONE case: no log while GitHub's own allowance is empty.
#
# zero_cost=true when the run billed nothing (total_cost_usd == 0, or no log): a
# PROVEN zero-billed failure the caller may retry for free on the SAME credential. A
# run that failed on the work itself carries a cost, so zero_cost=false there.
#
# wall_clock_only and content_refusal say the next credential meets the same answer,
# so the ladder stops. Each is false unless the fan-out aggregate sets it: every
# errored shard died at the timeout, or was refused by a content classifier.
#
# It also BILLS the log to METRICS.md's Claude-usage ledger. This decider follows every
# resolver rung, so billing here measures every Claude surface.
set -euo pipefail

errored=true
zero_cost=true
wall_clock_only=false
content_refusal=false
if [[ -n "${EXECUTION_FILE:-}" && -s "$EXECUTION_FILE" ]]; then
  # Never fails the decision: a missing metric point costs less than a refused
  # merge resolution. Its stdout is discarded rather than kept — nothing below
  # reads the recorder's chatter, and a kept stdout beside `|| true` is what
  # makes a real failure look like an answer. Its stderr still reaches the log.
  /usr/bin/python3 "$(dirname "${BASH_SOURCE[0]}")/record-claude-usage.py" \
    "$EXECUTION_FILE" >/dev/null || true
  result_jq='if type == "array" then (map(select(.type == "result")) | last) else . end'
  if ! jq -e "${result_jq} | .is_error == true" "$EXECUTION_FILE" >/dev/null; then
    errored=false
  fi
  if ! jq -e "${result_jq} | .total_cost_usd == 0" "$EXECUTION_FILE" >/dev/null; then
    zero_cost=false
  fi
  if jq -e "${result_jq} | .wall_clock_only == true" "$EXECUTION_FILE" >/dev/null; then
    wall_clock_only=true
  fi
  if jq -e "${result_jq} | .content_refusal == true" "$EXECUTION_FILE" >/dev/null; then
    content_refusal=true
  fi
else
  # No log: a dead credential and a GitHub refusal both leave none, and only the first
  # is the next rung's business. An empty GH_TOKEN allowance refused the action's first
  # call, so every other credential meets it: fail with GitHub's error and no verdict.
  # A reset inside the allowed wait is slept out. The probe's own refusal is evidence
  # too, because a budget this empty refuses `/rate_limit` as well.
  probe_refusal=""
  if ! probe_stderr=$(gh api rate_limit 2>&1 >/dev/null); then
    probe_refusal="$probe_stderr"
  fi
  # Three fixed lines: exhausted, the seconds to wait (empty means stop), the reason.
  # An unreadable verdict is no evidence of exhaustion, so the ladder goes on.
  if ! verdict=$(/usr/bin/python3 "$(dirname "${BASH_SOURCE[0]}")/_gh_rate_limit.py" 0 "$probe_refusal"); then
    verdict="false"
  fi
  exhausted=$(sed -n 1p <<<"$verdict")
  wait_secs=$(sed -n 2p <<<"$verdict")
  why=$(sed -n 3p <<<"$verdict")
  if [[ "$exhausted" == "true" && -n "$wait_secs" ]]; then
    echo "$why" >&2
    sleep "$wait_secs"
  elif [[ "$exhausted" == "true" ]]; then
    echo "::error::GitHub refused this attempt before any model ran, and no Claude credential changes that, so the credential ladder stops. ${why}" >&2
    exit 1
  fi
fi
{
  echo "errored=${errored}"
  echo "zero_cost=${zero_cost}"
  echo "wall_clock_only=${wall_clock_only}"
  echo "content_refusal=${content_refusal}"
} >>"$GITHUB_OUTPUT"
