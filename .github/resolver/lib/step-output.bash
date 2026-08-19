# shellcheck shell=bash
# kcov-exclude: a GitHub Actions step body that no test runs: it reads the runner's context
#   — GITHUB_*, a job-scoped GH_TOKEN, an actions/ working directory — or provisions the
#   runner itself, so it has no entry point off a runner.
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set
# shell options.
#
# One step output, written only when a runner gave the step a file to write to.
# The guard is what lets every one of these scripts run outside Actions — a
# behavioral suite drives them as plain `bash <script>`, where the variable is
# unset and an unguarded append would die on the redirect.

if [[ -z "${_STEP_OUTPUT_SOURCED:-}" ]]; then
  _STEP_OUTPUT_SOURCED=1

  # step_output NAME=VALUE — record one output for the steps that read this one.
  step_output() {
    if (($# != 1)); then
      echo "step_output: usage: step_output NAME=VALUE" >&2
      return 2
    fi
    if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
      printf '%s\n' "$1" >>"$GITHUB_OUTPUT"
    fi
  }
fi
