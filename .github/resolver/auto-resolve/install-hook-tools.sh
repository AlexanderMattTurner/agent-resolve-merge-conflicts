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

# The hook pins come from the CALLER's trusted base checkout, never from this
# repository: these packages provision the interpreter that runs the CALLER's
# pre-commit hooks over the merge, so a resolver-local pin would run the merge
# through a different shellcheck than the caller's own CI does.
_BASE_REPO_ROOT="${BASE_REPO_ROOT:?BASE_REPO_ROOT required — the trusted base checkout of the calling repository}"
_CALLER_PYPROJECT="${_BASE_REPO_ROOT}/pyproject.toml"

# The caller's Python pins, or NOTHING when it ships no pyproject.toml — a legal
# shape here. Reading the absent file answers a FileNotFoundError traceback, which
# reads as a resolver crash rather than as the caller's own shape.
_caller_py_specs() {
  [[ -f "$_CALLER_PYPROJECT" ]] || return 0
  python3 "$_SCRIPT_DIR/hook-py-specs.py" "$@" "$_CALLER_PYPROJECT"
}
_CALLER_TOOL_VERSIONS="${_BASE_REPO_ROOT}/.github/tool-versions.sh"
[[ -f "$_CALLER_TOOL_VERSIONS" ]] || {
  echo "::error::${_CALLER_TOOL_VERSIONS} does not exist, so the caller's shellcheck and shfmt pins cannot be read"
  exit 1
}
# shellcheck disable=SC1090  # the path is the caller's, resolved at run time
source "$_CALLER_TOOL_VERSIONS"
# OPTIONAL, both. A caller whose shellcheck and shfmt hooks come from pre-commit's
# own hook repositories pins neither, and demanding a pin there killed every
# resolve in such a repository at this step. A `language: system` hook that does
# need one and finds none still fails loud at the hook run.
_shellcheck_pin="${SHELLCHECK_PY_VERSION:-}"
_shfmt_pin="${SHFMT_VERSION:-}"
if [[ -z "$_shellcheck_pin" && -z "$_shfmt_pin" ]]; then
  echo "${_CALLER_TOOL_VERSIONS} pins neither SHELLCHECK_PY_VERSION nor SHFMT_VERSION, so this caller's hooks provision their own; installing no hook binary."
fi

: "${GITHUB_PATH:?GITHUB_PATH required}"

# Fail on the missing toolchain immediately: without this, an image that stopped
# shipping Go spends the whole retry backoff walking into the same wall and then
# reports a download failure rather than the absent compiler. Only what this run
# installs is demanded.
[[ -z "$_shellcheck_pin" ]] || _prereqs=(uv)
[[ -z "$_shfmt_pin" ]] || _prereqs+=(go)
for prereq in "${_prereqs[@]:-}"; do
  [[ -n "$prereq" ]] || continue
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

[[ -z "$_shellcheck_pin" ]] ||
  retry uv tool install --quiet "shellcheck-py==${_shellcheck_pin}"
# `go install` rather than a release tarball: tool-versions.sh pins no sha256 for
# shfmt because Go's checksum database is what proves this build's integrity.
[[ -z "$_shfmt_pin" ]] ||
  retry env GOBIN="$bin_dir" go install "mvdan.cc/sh/v3/cmd/shfmt@${_shfmt_pin}"

echo "$bin_dir" >>"$GITHUB_PATH"

# The redaction engine `bin/lib/transcript-publish.py` imports.
# Without it the log staging step publishes a REDACTION-FAILED
# placeholder and the JOB stays green, losing the only record of
# what a resolution did. `python3 -m pip` because the importer is
# `python3 "$REDACTOR"`.

# Pinning nothing is legal, so pip is never handed an empty
# requirement. Declaring a redactor while pinning nothing is the
# real fault, and it is named below.
sanitizer_req="$(_caller_py_specs --runtime)"
if [[ -n "$sanitizer_req" ]]; then
  retry python3 -m pip install --quiet "$sanitizer_req"
  python3 -c 'import agent_sanitizer.secrets' || {
    echo "::error::agent_sanitizer is not importable after installing ${sanitizer_req}, so this run's agent logs would publish as a redaction-failure placeholder"
    exit 1
  }
elif [[ -n "${AUTO_RESOLVE_LOG_REDACTOR:-}" ]]; then
  echo "::error::this caller sets log-redactor=${AUTO_RESOLVE_LOG_REDACTOR} but pins no agent-sanitizer in ${_CALLER_PYPROJECT}'s [project].dependencies, so the fan-out logs would publish as a redaction-failure placeholder. Pin it, or unset log-redactor to publish no logs."
  exit 1
else
  echo "this caller pins no agent-sanitizer and declares no log-redactor, so this run publishes no fan-out logs; installing no redaction engine."
fi

# A post-condition, not an exit status: an install that "succeeded" without leaving a
# runnable binary would hand finalize the exact missing-hook abort this step exists to
# prevent. Asserted at the pinned path rather than through `command -v`, which a runner's
# own drifting /usr/bin build would satisfy while our install produced nothing. Over what
# this run INSTALLED, never the pair: a caller pinning neither installs neither.
_installed=()
[[ -z "$_shellcheck_pin" ]] || _installed+=(shellcheck)
[[ -z "$_shfmt_pin" ]] || _installed+=(shfmt)
for tool in "${_installed[@]:-}"; do
  [[ -n "$tool" ]] || continue
  [[ -x "$bin_dir/$tool" ]] || {
    echo "::error::${tool} was not installed into ${bin_dir} despite its install command succeeding"
    exit 1
  }
  "$bin_dir/$tool" --version
done

# The `language: system` python hooks import these against the ambient interpreter,
# which is why the install targets it rather than a venv or a uv tool.
# hook-py-specs.py reads the versions out of the base ref's pyproject.toml, so its
# dev extra stays the one place they live. Each entry is `import-name:distribution`:
# the caller pins the distribution, this script asserts the import.
_HOOK_PY_MODULES=(
  tree_sitter:tree-sitter
  tree_sitter_bash:tree-sitter-bash
  tree_sitter_javascript:tree-sitter-javascript
  yaml:pyyaml
  pathspec:pathspec
)
# Captured through a command substitution so `set -e` sees hook-py-specs.py's status at
# this line. Reading it with `mapfile < <(…)` would not: mapfile reports its own status
# and pipefail does not cover process substitution, so a dropped pin would leave the
# array empty, hand pip zero requirements, and surface five retries later as a failure
# naming pip — burying the remedy this script exists to print.
hook_py_specs_raw="$(_caller_py_specs)"
# Guarded because `<<<` appends a newline to whatever it is given, so an EMPTY spec list
# mapfiles to an array of one EMPTY STRING rather than to an empty array. That element is
# what reaches pip as `''`, and pip answers `Invalid requirement: ''` — five times, once
# per retry, naming neither this caller nor the pin it is missing.
if [[ -n "$hook_py_specs_raw" ]]; then
  mapfile -t hook_py_specs <<<"$hook_py_specs_raw"
  retry python3 -m pip install --quiet "${hook_py_specs[@]}"
fi

# The same post-condition as the binaries above: pip reporting success while the
# interpreter cannot import the module must be a red HERE. Over what this run
# INSTALLED — a caller may pin a SUBSET, and demanding an unpinned module's import
# refuses every resolve in that repository. hook-py-specs.py answers which
# distributions those were, so PEP 503 normalization keeps one definition.
_installed_dists="$(_caller_py_specs --canonical)"
for entry in "${_HOOK_PY_MODULES[@]}"; do
  grep -qxF "${entry#*:}" <<<"$_installed_dists" || continue
  module="${entry%%:*}"
  python3 -c "import $module" 2>/dev/null || {
    echo "::error::python3 cannot import ${module} despite its install succeeding, so every pre-commit hook that imports it would abort and be read as a failed resolution"
    exit 1
  }
done
