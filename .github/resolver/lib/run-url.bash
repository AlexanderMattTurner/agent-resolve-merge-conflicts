# shellcheck shell=bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite: the suite runs the
#   calling scripts under `bash` against a localhost GitHub, so the branches are asserted
#   but no run is ever traced.
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set shell options.
#
# PROBLEM CLASS — a report that cites a log the reader cannot find. Auto-resolve
# dispatches one sharded run per pull request, and every run carries the same
# `display_title` and the same `head_branch`, so nothing on the Actions list says
# which run looked at which pull request. A comment that says "the job log holds
# what it reported" and links nothing therefore costs the reader every shard of
# every run in the window. This is the one definition of that link.

if [[ -z "${_RUN_URL_SOURCED:-}" ]]; then
  _RUN_URL_SOURCED=1

  # auto_resolve_run_url — this run's page, or empty off a runner. Every commit-status
  # mark carries it, because the mark outlives the sticky pull-request comment: a later
  # run overwrites that comment, and then the run that wrote the mark is the only
  # place the refusal's reasons still exist.
  auto_resolve_run_url() {
    [[ -n "${GITHUB_RUN_ID:-}" ]] || return 0
    printf '%s/%s/actions/runs/%s\n' \
      "${GITHUB_SERVER_URL:-https://github.com}" "${GITHUB_REPOSITORY:-}" "$GITHUB_RUN_ID"
  }
fi
