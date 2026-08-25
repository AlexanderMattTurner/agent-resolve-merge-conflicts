#!/usr/bin/env bash
# Emit `dir=` on $GITHUB_OUTPUT: the directory holding the resolver scripts.
#
# In the resolver's OWN repository the checkout already is the resolver, at the
# very sha under test, so it is read in place. Cloning there would fetch the
# last release instead and a PR that changes the renderer would render its own
# merge deltas with the previous renderer.
#
# Anywhere else the clone runs, pinned to the sha the caller's `uses:` names. A
# consumer also holds a synced .github/resolver, but that copy tracks the
# template's default branch and drifts out from under the pinned caller, so a
# consumer must never be served the in-tree copy.
#
# Env: GITHUB_OUTPUT, GITHUB_REPOSITORY, RUNNER_TEMP, RESOLVER_REPOSITORY.
set -euo pipefail

: "${GITHUB_OUTPUT:?GITHUB_OUTPUT required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY required — it decides whether this checkout IS the resolver}"
: "${RUNNER_TEMP:?RUNNER_TEMP required}"
: "${RESOLVER_REPOSITORY:?RESOLVER_REPOSITORY required — the repository that ships the resolver}"

RENDERER="remerge-diff-report.py"

# GitHub repository names are case-insensitive, and the two spellings reach this
# script from different places (a workflow literal and the event context).
if [[ "${GITHUB_REPOSITORY,,}" == "${RESOLVER_REPOSITORY,,}" ]]; then
  # A sparse checkout that omits .github/resolver is the failure this refusal
  # blocks: without it the read below dies later with a bare FileNotFoundError.
  [[ -f ".github/resolver/${RENDERER}" ]] || {
    echo "::error::${GITHUB_REPOSITORY} is the resolver, but this checkout has no .github/resolver/${RENDERER} — widen the job's sparse-checkout" >&2
    exit 1
  }
  printf 'dir=%s\n' "${PWD}/.github/resolver" >>"$GITHUB_OUTPUT"
  exit 0
fi

ref="$(python3 .github/scripts/resolver-ref.py)"
dest="${RUNNER_TEMP}/resolver"
timeout --kill-after=30 300 git clone --no-tags --no-checkout --filter=blob:none \
  "https://github.com/${RESOLVER_REPOSITORY}.git" "$dest"
timeout --kill-after=30 300 git -C "$dest" fetch --depth 1 origin "$ref"
git -C "$dest" checkout --detach FETCH_HEAD

[[ -f "${dest}/.github/resolver/${RENDERER}" ]] || {
  echo "::error::${RESOLVER_REPOSITORY}@${ref} carries no .github/resolver/${RENDERER}" >&2
  exit 1
}
printf 'dir=%s\n' "${dest}/.github/resolver" >>"$GITHUB_OUTPUT"
