#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body that no test runs: it reads the runner's context
#   — GITHUB_*, a job-scoped GH_TOKEN, an actions/ working directory — or provisions the
#   runner itself, so it has no entry point off a runner.
# Auto-resolve merge conflicts — MARK-SPEND step.
#
# Records, on the head commit this run resolves, that a run is about to bill a model
# against it. Runs immediately BEFORE the credential ladder, never after it: a run
# killed mid-ladder reaches no later step, and that is exactly the run whose spend a
# takeover must not repeat.
#
# The reader is mark-attempt.sh. It releases a stale attempt mark only when the head
# carries none of the marks auto_resolve_head_bought reads, so this write is what
# stops a takeover buying one tree twice.
# Env: GH_TOKEN, REPO, HEAD_SHA.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/resolver/lib-ci-retry.sh
source "$SCRIPT_DIR/../lib-ci-retry.sh"
# shellcheck source=.github/resolver/lib/auto-resolve-attempt.bash
source "$SCRIPT_DIR/../lib/auto-resolve-attempt.bash"

: "${REPO:?REPO required}"
: "${HEAD_SHA:?HEAD_SHA required}"

auto_resolve_mark_spend "$REPO" "$HEAD_SHA"
echo "Marked ${HEAD_SHA} as spent — a later run releases this head's attempt mark only if nothing bought it."
