#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body that no test runs: it reads the runner's context
#   — GITHUB_*, a job-scoped GH_TOKEN, an actions/ working directory — or provisions the
#   runner itself, so it has no entry point off a runner.
# Install @anthropic-ai/claude-code globally at the RESOLVER's pin.
# Env: NPM_INSTALL_TIMEOUT_SECONDS, NPM_INSTALL_KILL_AFTER_SECONDS,
# NPM_INSTALL_RETRY_DELAY_MS tune the bound below.
set -euo pipefail

# The pin comes from this script's OWN repository, never from the working
# directory. Both callers `cd` somewhere first — the self-review into the calling
# repository's base, the conflict resolver into this one — and a version read
# from the caller's tree would let a repository this resolver merges for choose
# which CLI binary runs the merge.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Two levels: the template syncs a default pin to every repository, and a
# repository's own Dependabot bumps its override. The override wins when present.
# allow-unsynced: .github/claude-cli/package.json — each repository's own Dependabot bumps it; absent, the synced default applies.
override="${SCRIPT_DIR}/../claude-cli/package.json"
default="${SCRIPT_DIR}/../claude-cli-default/package.json"
if [[ -f "$override" ]]; then
  pin_file="$override"
elif [[ -f "$default" ]]; then
  pin_file="$default"
else
  echo "neither ${override} nor ${default} exists: the resolver has no pinned claude-code version to install" >&2
  exit 1
fi
command -v jq >/dev/null 2>&1 || {
  echo "jq is required to read ${pin_file}" >&2
  exit 1
}
if ! version="$(jq -r '.dependencies["@anthropic-ai/claude-code"]' "$pin_file")"; then
  echo "${pin_file} is not valid JSON" >&2
  exit 1
fi
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "${pin_file} pins @anthropic-ai/claude-code at '${version}'; it must be an exact X.Y.Z version" >&2
  exit 1
fi
# Idempotent: a claude already at the pin needs no install. This is what makes
# the install-claude-cli action's cache restore work — a restored global tree
# answers with the pinned version and the registry is never contacted.
if [[ "$(claude --version 2>/dev/null || true)" == "$version"* ]]; then
  echo "@anthropic-ai/claude-code@${version} already installed; skipping"
  exit 0
fi
echo "Installing @anthropic-ai/claude-code@${version}"
# Bound + retry: a bare `npm install -g` has no timeout, so a hung registry connection
# stalls here until the job's own timeout cancels it. --kill-after is what makes the cap
# real: `timeout` alone sends only SIGTERM, and an npm blocked on a dead registry socket
# takes minutes to act on it. The ladder must fit INSIDE the tightest caller's budget —
# validate-config.yaml's `validate` job budgets 20 min total for setup, config
# validation and the pytest run together, and this spends 310 s worst case.
# shellcheck source=.github/resolver/lib-ci-retry.sh
source "${SCRIPT_DIR}/lib-ci-retry.sh"
RETRY_MAX=2 RETRY_BASE_DELAY="$(retry_delay_seconds "${NPM_INSTALL_RETRY_DELAY_MS:-10000}")" \
  retry \
  timeout --verbose --kill-after="${NPM_INSTALL_KILL_AFTER_SECONDS:-30}" \
  "${NPM_INSTALL_TIMEOUT_SECONDS:-120}" \
  npm install -g "@anthropic-ai/claude-code@${version}"
claude --version
