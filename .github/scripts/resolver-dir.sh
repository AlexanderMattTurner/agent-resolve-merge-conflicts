#!/usr/bin/env bash
# Emit `dir=` on $GITHUB_OUTPUT: the directory holding the resolver scripts.
#
# In the resolver's OWN repository the checkout already is the resolver, at the
# very sha under test, so it is read in place. Cloning there would fetch the
# last release instead and a PR that changes the resolver would run the
# previous release's copy against its own diff.
#
# Anywhere else the clone runs, pinned to the sha the caller's `uses:` names. A
# consumer also holds a synced .github/resolver, but that copy tracks the
# template's default branch and drifts out from under the pinned caller, so a
# consumer must never be served the in-tree copy.
#
# Env: GITHUB_OUTPUT, GITHUB_REPOSITORY, RUNNER_TEMP, RESOLVER_REPOSITORY,
# RESOLVER_PATHS, and RESOLVER_SPARSE_PATHS. The two lists answer different
# questions, and a job that merges them gets one of them wrong:
#
#   RESOLVER_PATHS        the resolver-relative paths this caller's own steps
#                         NAME. Checked against every served tree, because the
#                         caller can be newer than the pin it is served.
#   RESOLVER_SPARSE_PATHS the import closure behind those paths. Checked ONLY
#                         against a sparse in-repo checkout, the one served tree
#                         a file can go missing from while the commit is whole.
#                         A whole commit agrees with itself, so demanding its
#                         closure would refuse a working tree.
set -euo pipefail

: "${GITHUB_OUTPUT:?GITHUB_OUTPUT required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY required — it decides whether this checkout IS the resolver}"
: "${RUNNER_TEMP:?RUNNER_TEMP required}"
: "${RESOLVER_REPOSITORY:?RESOLVER_REPOSITORY required — the repository that ships the resolver}"
: "${RESOLVER_PATHS:?RESOLVER_PATHS required — name the resolver files this job reads}"

# A served tree missing one of them is the late bare "No such file" this
# refusal blocks — a sparse-checkout too narrow, or a pin older than the file.
require_paths() {
  local root="$1" label="$2" list="$3" rel
  local -a wanted
  read -ra wanted <<<"$list"
  [[ ${#wanted[@]} -gt 0 ]] || {
    echo "::error::${label} is blank — name the resolver files this job reads" >&2
    return 1
  }
  for rel in "${wanted[@]}"; do
    [[ -f "${root}/${rel}" ]] || {
      echo "::error::${root} carries no ${rel}, which this job reads" >&2
      return 1
    }
  done
}

# GitHub repository names are case-insensitive, and the two spellings reach this
# script from different places (a workflow literal and the event context).
if [[ "${GITHUB_REPOSITORY,,}" == "${RESOLVER_REPOSITORY,,}" ]]; then
  require_paths ".github/resolver" RESOLVER_PATHS "$RESOLVER_PATHS"
  # Asked of git rather than of the caller: a job that adds a sparse checkout
  # and forgets the closure list would otherwise serve a tree whose missing
  # module surfaces as a bare ImportError, which is what this refusal replaces.
  if [[ "$(git config --get --type=bool --default false core.sparseCheckout)" == "true" ]]; then
    : "${RESOLVER_SPARSE_PATHS:?RESOLVER_SPARSE_PATHS required — a sparse checkout must name the import closure it fetches}"
    require_paths ".github/resolver" RESOLVER_SPARSE_PATHS "$RESOLVER_SPARSE_PATHS"
  fi
  printf 'dir=%s\n' "${PWD}/.github/resolver" >>"$GITHUB_OUTPUT"
  exit 0
fi

ref="$(python3 .github/scripts/resolver-ref.py)"
dest="${RUNNER_TEMP}/resolver"
# 60 seconds each, so the pair cannot outlast 120 of the 300 the tightest
# calling job has. Past its own timeout-minutes GitHub cancels the job, and a
# cancelled job runs no fallback step and reports nothing to the pull request.
timeout --kill-after=30 60 git clone --no-tags --no-checkout --filter=blob:none \
  "https://github.com/${RESOLVER_REPOSITORY}.git" "$dest"
timeout --kill-after=30 60 git -C "$dest" fetch --depth 1 origin "$ref"
git -C "$dest" checkout --detach FETCH_HEAD

require_paths "${dest}/.github/resolver" RESOLVER_PATHS "$RESOLVER_PATHS"
printf 'dir=%s\n' "${dest}/.github/resolver" >>"$GITHUB_OUTPUT"
