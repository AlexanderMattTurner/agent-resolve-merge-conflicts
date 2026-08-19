#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite that COPIES it: the
#   suite copies this script into a scratch dir and runs the copy, so no invocation ever
#   names the tracked path and no run is traced.
# PROBLEM CLASS — a merge-queue gate that reads ONE PR number out of the queue ref and reports that verdict for the whole batch. The queue validates a batch once, at the batch head, and the ephemeral ref gh-readonly-queue/<base>/pr-<N>-<sha> carries only the LAST member's number, so gating that one PR merges every earlier member with its gate unevaluated. Each member contributes exactly one "Merge pull request #N …" commit between the merge group's base_sha and head_sha, so those merge-commit numbers ARE the batch.
#
# Env: GH_TOKEN, GH_REPO, MG_REF, MG_BASE_SHA, MG_HEAD_SHA.
set -euo pipefail

: "${GH_REPO:?GH_REPO required}"
: "${GH_TOKEN:?GH_TOKEN required}"
: "${MG_REF:?MG_REF required}"
: "${MG_BASE_SHA:?MG_BASE_SHA required}"
: "${MG_HEAD_SHA:?MG_HEAD_SHA required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/resolver/lib-ci-retry.sh
source "$(cd "$SCRIPT_DIR/../resolver" && pwd)/lib-ci-retry.sh"

# INVARIANT — this refuses rather than narrowing. Zero parsed members, or a roster missing the ref-named PR, means the queue's merge strategy or ref format drifted from what this reader assumes. A silently short roster gates fewer PRs than the batch merges, which is a fail-open on every member it dropped, so both cases are a loud red.
if [[ ! "$MG_REF" =~ /pr-([0-9]+)- ]]; then
  echo "cannot parse a PR number from merge-group ref '${MG_REF}'" >&2
  exit 1
fi
ref_pr="${BASH_REMATCH[1]}"

members="$(retry_stdout gh api "repos/${GH_REPO}/compare/${MG_BASE_SHA}...${MG_HEAD_SHA}" \
  --jq '[.commits[].commit.message
    | capture("^Merge pull request #(?<n>[0-9]+) ")? | .n]
    | unique | join(" ")')"
if [[ -z "$members" ]]; then
  echo "no batch members parsed between ${MG_BASE_SHA} and ${MG_HEAD_SHA} — refusing to gate an unreadable batch" >&2
  exit 1
fi
if [[ " ${members} " != *" $ref_pr "* ]]; then
  echo "batch roster (${members}) does not contain the ref-named PR #${ref_pr} — the roster read drifted from the queue's format" >&2
  exit 1
fi

# A red on one member reds the whole batch, which is the merge queue's own contract: it merges the batch as one unit.
read -ra roster <<<"$members"
failed=""
for n in "${roster[@]}"; do
  echo "── review-findings gate: gating PR #${n} (batch of ${#roster[@]}) ──"
  # allow-path-shadowed-interpreter: this job installs no venv, so there is no repo
  # interpreter to name, and the gate's import closure is stdlib-only.
  if ! PR="$n" python3 "${SCRIPT_DIR}/review_findings_gate.py"; then
    failed="${failed} #${n}"
  fi
done
if [[ -n "$failed" ]]; then
  echo "review-findings gate red for:${failed} — the batch cannot merge" >&2
  exit 1
fi
echo "review-findings gate green for all ${#roster[@]} batch member(s)"
