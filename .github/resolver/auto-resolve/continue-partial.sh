#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body that no test runs: it reads the runner's context
#   — GITHUB_*, a job-scoped GH_TOKEN, an actions/ working directory — or provisions the
#   runner itself, so it has no entry point off a runner.
# Auto-resolve merge conflicts — CONTINUE-PARTIAL step.
#
# Dispatch the next round against the same head, when this run resolved part of a
# conflict set and ran out of window before the rest.
#
# The round it dispatches starts from what this one resolved: the artifact this run
# uploads carries `salvage.patch` and `salvage.json`, and the next run's reuse step
# hands them to prepare, which stages those paths before it reads the conflict list.
# Without this dispatch the carry never happens — the head is marked handed off, so
# no ordinary scan selects the PR again.
#
# catch-up=true is what clears that mark. The bounds live in _marker_verdict's
# `continue_partial`, which sets CARRY_CONTINUE only while the chain is under its cap
# AND this round resolved more paths than the round it carried.
#
# A failed dispatch exits non-zero: the chain stops here and the pull request keeps
# a conflict nothing retries.
#
# Env: GH_TOKEN, PR, CARRY_CONTINUE, CARRY_ROUND, DISPATCH_REF.
set -euo pipefail

: "${PR:?PR required}"

if [[ "${CARRY_CONTINUE:-}" != "true" ]]; then
  exit 0
fi

if gh workflow run auto-resolve-conflicts.yaml \
  --ref "${DISPATCH_REF:?DISPATCH_REF required to dispatch the next round}" \
  -f pr="$PR" \
  -f catch-up=true; then
  echo "::notice::round ${CARRY_ROUND:-?} resolved part of this conflict set and ran out of window. Dispatched the next round, which starts from what this one resolved."
else
  echo "::error::round ${CARRY_ROUND:-?} resolved part of this conflict set, but dispatching the next round failed. This head keeps its handoff mark, so the remainder waits for a human or a push."
  exit 1
fi
