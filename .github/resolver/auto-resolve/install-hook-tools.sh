#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body that no test runs: it reads the runner's context
#   — GITHUB_*, a job-scoped GH_TOKEN, an actions/ working directory — or provisions the
#   runner itself, so it has no entry point off a runner.
# Install what `pre-commit run --files` needs in order to actually RUN over a
# resolved conflict: the external hook binaries, and the Python packages the
# `language: system` python hooks import — both at the versions this repo pins.
#
# Load-bearing, not a convenience. A `language: system` hook runs against the AMBIENT
# interpreter and PATH, so pre-commit provisions nothing for it: a missing binary is
# reported as a hook failure, and a missing import aborts the hook with a
# ModuleNotFoundError. Either way the resolver reads a red and refuses the resolution with
# "the resolved content fails the repo's pre-commit hooks", blaming the merge for a
# provisioning fault, on a job that deliberately skips `uv sync` so a pull request cannot
# choose what a write-token job installs. Only hard-failing hooks are covered;
# scripts/shellharden-run.sh and scripts/gitleaks-staged.sh skip themselves loudly, so a
# missing shellharden or gitleaks cannot abort a resolution.
#
# THE PINS COME FROM THE CALLING REPOSITORY'S TRUSTED BASE REF, never from the
# checked-out PR head: this job carries a write token, and a PR that edited
# .github/tool-versions.sh or pyproject.toml would otherwise choose the version and
# download source of something this job installs and then executes. `BASE_REPO_ROOT`
# is that base checkout. Paths relative to this script reach the RESOLVER's tree,
# which pins a different hook set at different versions.
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/resolver/lib-ci-retry.sh
source "$_SCRIPT_DIR/../lib-ci-retry.sh"

_CALLER_ROOT="${BASE_REPO_ROOT:?BASE_REPO_ROOT required — the trusted base checkout of the calling repository}"
_CALLER_PYPROJECT="${_CALLER_ROOT}/pyproject.toml"
_CALLER_TOOL_VERSIONS="${_CALLER_ROOT}/.github/tool-versions.sh"

[[ -f "$_CALLER_TOOL_VERSIONS" ]] || {
  echo "::error::${_CALLER_TOOL_VERSIONS} is missing, so the shellcheck and shfmt versions this repository's pre-commit hooks run against are unknown. Pin them there as SHELLCHECK_PY_VERSION and SHFMT_VERSION."
  exit 1
}
# shellcheck source=/dev/null
source "$_CALLER_TOOL_VERSIONS"

: "${GITHUB_PATH:?GITHUB_PATH required}"

# Fail on the missing toolchain immediately: without this, an image that stopped
# shipping Go spends the whole retry backoff walking into the same wall and then
# reports a download failure rather than the absent compiler.
for prereq in uv go; do
  command -v "$prereq" >/dev/null || {
    echo "::error::${prereq} is not on PATH, so the pinned hook binaries cannot be installed"
    exit 1
  }
done

bin_dir="$HOME/.local/bin"
install -d "$bin_dir"
[[ -d "$bin_dir" ]] || {
  echo "::error::could not create ${bin_dir} for the pinned hook binaries"
  exit 1
}

# Named in the failure text rather than left to `set -u`, whose bare "unbound
# variable" reports a line in the RESOLVER for a pin missing from the caller's file.
_pin_required="is not set in ${_CALLER_TOOL_VERSIONS}, so the version this repository's pre-commit hooks run against is unknown"
retry uv tool install --quiet "shellcheck-py==${SHELLCHECK_PY_VERSION:?$_pin_required}"
# `go install` rather than a release tarball: tool-versions.sh pins no sha256 for
# shfmt because Go's checksum database is what proves this build's integrity.
retry env GOBIN="$bin_dir" go install "mvdan.cc/sh/v3/cmd/shfmt@${SHFMT_VERSION:?$_pin_required}"

echo "$bin_dir" >>"$GITHUB_PATH"

# The redaction engine `bin/lib/transcript-publish.py` imports. Without it the log staging
# step exits with a bare `ModuleNotFoundError: No module named 'agent_sanitizer'` and
# publishes a REDACTION-FAILED placeholder in place of the fan-out logs — which
# stage-agent-logs.sh does by design, so the JOB stays green and nothing says the evidence
# is gone. Those per-shard logs are the only record of what one resolution did.
#
# The requirement is read from the TRUSTED base ref's pyproject.toml — the same boundary
# the pins above hold — through the same hook-py-specs.py the hook modules below go
# through, so the pin has one home and one PEP 503 matcher. Installed with `python3 -m pip`
# because the importer is `python3 "$REDACTOR"`, so the requirement has to land in whatever
# python3 resolves to rather than in an interpreter uv chose.
sanitizer_req="$(
  python3 "$_SCRIPT_DIR/hook-py-specs.py" --runtime "$_CALLER_PYPROJECT"
)"
retry python3 -m pip install --quiet "$sanitizer_req"
python3 -c 'import agent_sanitizer.secrets' || {
  echo "::error::agent_sanitizer is not importable after installing ${sanitizer_req}, so this run's agent logs would publish as a redaction-failure placeholder"
  exit 1
}

# A post-condition, not an exit status: an install that "succeeded" without leaving
# a runnable binary would hand finalize the exact missing-hook abort this step exists
# to prevent, and it must be a red HERE where the cause is legible. Asserted at the
# pinned path rather than through `command -v`, which a runner's own drifting
# /usr/bin build would satisfy while our install had silently produced nothing.
for tool in shellcheck shfmt; do
  [[ -x "$bin_dir/$tool" ]] || {
    echo "::error::${tool} was not installed into ${bin_dir} despite its install command succeeding"
    exit 1
  }
done
"$bin_dir/shellcheck" --version
"$bin_dir/shfmt" --version

# The `language: system` python hooks import these; pre-commit gives them no
# environment, so they resolve against the same interpreter `entry: python3` picks —
# which is why the install targets that interpreter rather than a venv or a uv tool.
# hook-py-specs.py reads the versions out of the base ref's pyproject.toml, so its
# dev extra stays the one place they live.
_HOOK_PY_MODULES=(tree_sitter tree_sitter_bash tree_sitter_javascript yaml pathspec)
# Captured through a command substitution so `set -e` sees hook-py-specs.py's status at
# this line. Reading it with `mapfile < <(…)` would not: mapfile reports its own status
# and pipefail does not cover process substitution, so a dropped pin would leave the
# array empty, hand pip zero requirements, and surface five retries later as a failure
# naming pip — burying the remedy this script exists to print.
hook_py_specs_raw="$(
  python3 "$_SCRIPT_DIR/hook-py-specs.py" "$_CALLER_PYPROJECT"
)"
mapfile -t hook_py_specs <<<"$hook_py_specs_raw"

retry python3 -m pip install --quiet "${hook_py_specs[@]}"

# The same post-condition discipline as the binaries above: pip reporting success
# while the hook interpreter still cannot import the module would hand the resolver
# the exact traceback-as-hook-failure this step exists to prevent, and it must be a
# red HERE, where the cause is legible, rather than 90 lines into a pre-commit log.
for module in "${_HOOK_PY_MODULES[@]}"; do
  python3 -c "import $module" 2>/dev/null || {
    echo "::error::python3 cannot import ${module} despite its install succeeding, so every pre-commit hook that imports it would abort and be read as a failed resolution"
    exit 1
  }
done
