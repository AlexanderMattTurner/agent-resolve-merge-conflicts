#!/usr/bin/env bash
# Install the locked Python dependency set with uv, hardened against a transient
# PyPI failure. Extra args pass through to `uv sync` (e.g. `--extra dev`);
# `--frozen` is always applied, because every CI leg installs the locked set.
#
# PROBLEM CLASS — a package download that stalls or drops reds a CI job that only
# wanted to install its dependencies.
#
# uv's own budget is 3 attempts against a 30 s per-request timeout, and it spends
# all 3 back to back. A registry or CDN stall longer than that exhausts the budget
# in about two minutes and fails the job. This raises the per-request timeout and
# uv's retry count, then retries the whole sync through the shared ladder, so an outage of
# tens of seconds still ends in a green install.
set -euo pipefail

# A SIBLING, not a path off the repo root: the project this syncs is named by
# `--project`, and it is a different repository from the one shipping this
# script. Deriving the helper from the root would look for it in the tree being
# synced, which is exactly the tree that does not have to carry the resolver.
# shellcheck source=.github/resolver/lib-ci-retry.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib-ci-retry.sh"

# The caller owns all three budgets; these are the values a leg that sets none
# installs under. The WSL2 legs reach PyPI over the distro's NAT bridge, where a
# wheel fetch that costs 3 s on a Linux runner can cost ten times that — so uv's
# 30 s default is a timeout those legs hit on a healthy network.
export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-180}"
export UV_HTTP_RETRIES="${UV_HTTP_RETRIES:-5}"

# 3 attempts at 20 s doubling: 20 s then 40 s, so the old --max-delay-ms 60000 cap
# never bound and is not carried over. Raising RETRY_MAX here would need it back.
RETRY_MAX=3 RETRY_BASE_DELAY="$(retry_delay_seconds "${UV_SYNC_RETRY_DELAY_MS:-20000}")" \
  retry uv sync --frozen "$@"
