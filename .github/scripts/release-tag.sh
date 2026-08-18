#!/bin/bash
# Tag a release for a repository that publishes NO package.
#
# This repository ships a reusable workflow. A caller reaches it by SHA, so
# there is nothing to push to a registry — the git tag IS the release. Two tags
# move per release: an immutable `vX.Y.Z`, and a `v1` that this script advances
# to the same commit. `v1` is not a supply chain to depend on; it is what makes
# a bump reviewable, because a caller can see that a compatible release exists
# and then update its pinned SHA deliberately.
#
# The version source is the latest `vX.Y.Z` tag, not package.json and not a
# registry. package.json here is `"private": true` and carries version 1.0.0
# forever, so reading it would re-release the same number on every push.
#
# DRY RUN IS THE DEFAULT. Set RELEASE_DRY_RUN=false to make it tag and push.
# The workflow leaves it on until a human has watched one live cycle print the
# version it would have cut; the commit that flips the default is the swap.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log() { echo "$@" >&2; }

DRY_RUN="${RELEASE_DRY_RUN:-true}"
MAJOR_TAG="${RELEASE_MAJOR_TAG:-v1}"

# Bump level from Conventional Commits. A breaking marker caps at MINOR rather
# than major: this repository's callers pin by SHA and read `v1`, so an
# automated major would strand every one of them behind a tag that stops
# moving. A real major is a deliberate, manual act.
determine_bump() {
  local subjects="$1" full_messages="$2"
  if grep -Eq '^[a-zA-Z]+(\([^)]*\))?!:' <<<"$subjects" ||
    grep -Eq '^BREAKING[- ]CHANGE:' <<<"$full_messages"; then
    log "Breaking-change marker found. Automated major bumps are disabled — capping at 'minor'."
    echo "minor"
  elif grep -Eq '^feat(\([^)]*\))?:' <<<"$subjects"; then
    echo "minor"
  else
    echo "patch"
  fi
}

LAST_TAG=$(git describe --tags --match "v[0-9]*.[0-9]*.[0-9]*" --abbrev=0 HEAD 2>/dev/null || echo "") # echo-fallback-ok: no tag yet is the first-release state, and the `if [[ -n "$LAST_TAG" ]]` below is what handles it
HEAD_SHA=$(git rev-parse HEAD)

if [[ -n "$LAST_TAG" ]]; then
  if [[ "$(git rev-list -1 "$LAST_TAG")" = "$HEAD_SHA" ]]; then
    log "HEAD is already $LAST_TAG. Nothing to release."
    exit 0
  fi
  CURRENT_VERSION="${LAST_TAG#v}"
  RANGE="$LAST_TAG..HEAD"
else
  CURRENT_VERSION=""
  RANGE="HEAD"
  log "No vX.Y.Z tag found. Cutting the first release."
fi

SUBJECTS=$(git log "$RANGE" --pretty=format:%s --no-merges)
MESSAGES=$(git log "$RANGE" --pretty=format:%B --no-merges)

# A release-docs commit is this script's own output. Releasing on top of it
# would cut a version whose only content is the previous version's changelog,
# once per push, forever.
if [[ -n "$SUBJECTS" ]] && ! grep -qvE '^chore\(release\):' <<<"$SUBJECTS"; then
  log "Only release-docs commits since $LAST_TAG. Nothing to release."
  exit 0
fi

if [[ -z "$CURRENT_VERSION" ]]; then
  # The first release is 1.0.0, not 0.1.0: `v1` must name something the moment
  # it first moves, and a 0.x line would leave it pointing at nothing.
  NEW_VERSION="1.0.0"
else
  BUMP=$(determine_bump "$SUBJECTS" "$MESSAGES")
  IFS='.' read -r MAJOR MINOR PATCH <<<"$CURRENT_VERSION"
  case "$BUMP" in
  minor) NEW_VERSION="${MAJOR}.$((MINOR + 1)).0" ;;
  patch) NEW_VERSION="${MAJOR}.${MINOR}.$((PATCH + 1))" ;;
  *)
    log "Error: unexpected bump level '$BUMP'. Refusing to guess a version."
    exit 1
    ;;
  esac
fi

if ! [[ "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  log "Error: invalid version '$NEW_VERSION'."
  exit 1
fi

# `v1` must name the major this release actually carries. A mismatch means the
# major moved by hand and RELEASE_MAJOR_TAG was not updated with it; advancing
# `v1` onto a 2.x commit would silently break every caller reading it.
NEW_MAJOR_TAG="v${NEW_VERSION%%.*}"
if [[ "$NEW_MAJOR_TAG" != "$MAJOR_TAG" ]]; then
  log "Error: this release is $NEW_VERSION but RELEASE_MAJOR_TAG is $MAJOR_TAG. Refusing to move it."
  exit 1
fi

log "Release: $NEW_VERSION (from ${LAST_TAG:-nothing}) at $HEAD_SHA; $MAJOR_TAG follows."

{
  echo "version=$NEW_VERSION"
  echo "released=$([[ "$DRY_RUN" == "true" ]] && echo false || echo true)"
} >>"${GITHUB_OUTPUT:-/dev/null}"

if [[ "$DRY_RUN" == "true" ]]; then
  log "RELEASE_DRY_RUN is true. No tag was created and nothing was pushed."
  exit 0
fi

RELEASE_DATE=$(date -u +%Y-%m-%d)
CHANGELOG_SECTION=$(git log "$RANGE" --pretty=format:"- %s" --no-merges | awk 'NR <= 40')

NEW_VERSION="$NEW_VERSION" RELEASE_DATE="$RELEASE_DATE" CHANGELOG_SECTION="$CHANGELOG_SECTION" \
  node "$SCRIPT_DIR/promote-changelog.mjs"

git add CHANGELOG.md
if ! git diff --cached --quiet; then
  git commit -m "chore(release): v$NEW_VERSION [skip ci]"
fi

git tag "v$NEW_VERSION"
# `--force` on the major tag only. It is defined as moving; the vX.Y.Z tag is
# never re-pointed, so a re-run that reaches an existing version fails here
# rather than rewriting a release.
git tag --force "$MAJOR_TAG"

git push origin HEAD "v$NEW_VERSION"
git push origin --force "refs/tags/$MAJOR_TAG"
log "Pushed v$NEW_VERSION and moved $MAJOR_TAG to $(git rev-parse HEAD)."
