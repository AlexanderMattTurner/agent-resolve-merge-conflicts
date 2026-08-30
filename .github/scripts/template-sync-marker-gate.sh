#!/usr/bin/env bash
# Refuse a template-sync branch that still carries conflict markers.
#
# PROBLEM CLASS — raw diff3 markers committed as if they were a resolution.
# template-sync.sh writes `<<<<<<< local` … `>>>>>>> template` into the tree on
# purpose: those markers are what the two resolver tiers read. But
# `create-pull-request` commits that tree BEFORE either tier runs, so a file
# neither tier settles reaches the branch with its markers intact. Nothing then
# failed — template-sync-resolve.sh only warns about its `unresolved` set — so
# the run stayed green while a marked bash library sat on the branch and the
# consumer's own CI died on `<<<` as a syntax error.
#
# This gate restores every still-marked path to its pre-sync content and pushes
# that, so the branch head carries no marker whatever the tiers managed. It then
# exits 1, which reds the run: "Sync from Template" is in ci-failure-notify's
# watched list. The marker text stays in the PR body's conflict report, which is
# where a human resolves it from.
#
# Env: BASE_SHA (the commit the sync branched from), CONFLICT_FILES (the
# sync's own space-separated conflict_files output, possibly empty),
# GITHUB_TOKEN.
set -euo pipefail

# Read before the sources below, which need `jq` on PATH: a missing tool must
# not stand in for a missing variable in the failure this script reports.
: "${BASE_SHA:?BASE_SHA required}"
: "${CONFLICT_FILES?CONFLICT_FILES required (empty string is fine, unset is not)}"
: "${GITHUB_TOKEN:?GITHUB_TOKEN required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOLVER_DIR="$(cd "${SCRIPT_DIR}/../resolver" && pwd)"
# committed_marker_paths and CONFLICT_MARKER_RE from one place; git_auth_header
# from another.
# shellcheck source=.github/resolver/auto-resolve/lib.sh
source "${RESOLVER_DIR}/auto-resolve/lib.sh"
# shellcheck source=.github/resolver/lib/git-auth.bash
source "${RESOLVER_DIR}/lib/git-auth.bash"

BRANCH="template-sync"

git_auth_header "$GITHUB_TOKEN"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# The branch, not the workspace, is what a consumer checks out. `-f` is what
# makes that true: the resolve step leaves edits it chose not to push, and a
# plain checkout would carry them in and judge a state nobody can fetch.
timeout --kill-after=30 300 git fetch --no-tags origin "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}"
git checkout -q -f -B "$BRANCH" "origin/${BRANCH}"

# committed_marker_paths requires the COMPLETE marker triple per file, and drops
# a path whose base copy already carries the same block, so a repository that
# keeps marker text as a fixture is never withheld from itself. But it greps the
# WHOLE tree, so it also names a path the template shipped legitimately (a new
# fixture file, first sync, no base copy to compare against) if that path merely
# CONTAINS marker-shaped text. Intersecting with CONFLICT_FILES — the sync's own
# record of which paths its merge actually conflicted on — is what keeps this
# gate's destructive branch (below) off a file that was never one of those.
read -ra conflict_files <<<"$CONFLICT_FILES"
declare -A is_conflict_file=()
for path in "${conflict_files[@]}"; do
  is_conflict_file["$path"]=1
done

marked=()
while IFS= read -r path; do
  [[ -n "$path" ]] || continue
  [[ -n "${is_conflict_file[$path]:-}" ]] || continue
  marked+=("$path")
done < <(committed_marker_paths "$BASE_SHA")

if [[ ${#marked[@]} -eq 0 ]]; then
  echo "template-sync-marker-gate: no conflict markers on ${BRANCH}."
  exit 0
fi

echo "Conflict markers committed on ${BRANCH}:"
git grep -nE "$CONFLICT_MARKER_RE" -- "${marked[@]}"

# A marked path always existed locally before the sync, because template-sync.sh
# writes markers only where it found a local file. So the base copy is the state
# the adopter had. A path the base lacks can only be one a resolver tier created,
# and there is no earlier content to go back to.
for path in "${marked[@]}"; do
  if git cat-file -e "${BASE_SHA}:${path}" 2>/dev/null; then
    git checkout "$BASE_SHA" -- "$path"
  else
    git rm -q -f -- "$path"
  fi
done

git commit -q -m "chore: withhold ${#marked[@]} unresolved template-sync file(s)

The resolver left conflict markers in these files, so the sync keeps this
repository's own copy of each. The marked version is the previous commit on
${BRANCH}; recover it with 'git show HEAD~1:<path>' and resolve by hand.

${marked[*]}"
timeout --kill-after=30 300 git push origin "HEAD:${BRANCH}"

echo "::error::template-sync-marker-gate: ${#marked[@]} file(s) reached ${BRANCH} carrying conflict markers: ${marked[*]}. Each is back to this repository's pre-sync copy; the marked version is the previous commit on ${BRANCH}, so read it with 'git show HEAD~1:<path>' and resolve by hand."
exit 1
