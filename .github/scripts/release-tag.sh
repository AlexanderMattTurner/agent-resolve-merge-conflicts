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
# DRY RUN IS THE DEFAULT, and it fails CLOSED: only the exact string "false"
# makes this tag and push. `TRUE`, `1` and `yes` are all plausible ways to ask
# FOR a dry run, so anything that is not exactly "false" stays dry rather than
# force-pushing a tag on a typo. The workflow passes "false" unless the
# RELEASE_DRY_RUN repository variable overrides it, so an invocation that sets
# nothing — a local run — tags nothing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log() { echo "$@" >&2; }

LIVE="${RELEASE_DRY_RUN:-true}"
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

# The major tag is pushed after the version tag, and that second push can fail
# on its own. Repairing it on every exit that cuts no version is what stops one
# rejected push from leaving `v1` stale until someone notices.
repair_major_tag() {
  [[ -n "$LAST_TAG" ]] || return 0
  [[ "$LIVE" == "false" ]] || return 0
  local released_sha tagged_major
  released_sha="$(git rev-list -1 "$LAST_TAG")"
  # The major the existing tag actually names. Without this an operator whose
  # line has moved to 2.x, with RELEASE_MAJOR_TAG still v1, would have v1
  # dragged onto a 2.x commit — the case the guard further down refuses.
  tagged_major="v${LAST_TAG#v}"
  tagged_major="${tagged_major%%.*}"
  [[ "$tagged_major" == "$MAJOR_TAG" ]] || return 0
  [[ "$(git rev-list -1 "$MAJOR_TAG" 2>/dev/null || echo none)" != "$released_sha" ]] || return 0 # echo-fallback-ok: an absent major tag must compare unequal so the repair below runs; `none` is never a sha
  log "$MAJOR_TAG is behind $LAST_TAG. Moving $MAJOR_TAG."
  git tag --force "$MAJOR_TAG" "$released_sha"
  git push origin --force "refs/tags/$MAJOR_TAG"
}

# `git commit` below needs an identity, and actions/checkout does not set one.
configure_git_identity() {
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
}

# ERE-escaped, because the reference carries dots. Interpolated bare, each one
# matches any character, so a malformed `uses:` path passes the count and takes
# the rewrite — leaving a line that reads as a pin and resolves to nothing.
ere_escape() { printf '%s' "$1" | sed 's/[]\[(){}.*+?^$|]/\\&/g'; }

pin_reference=""
pin_reference_re=""
pin_files=()

# Which files pin THIS repository's resolver, and how many pins each must hold.
collect_pin_files() {
  # allow-unsynced: .github/workflows/auto-resolve-conflicts.yaml — each consumer writes its own caller, and a consumer's caller pins THIS repository's releases rather than its own, so only the repository that owns the resolver rewrites the line.
  local caller=".github/workflows/auto-resolve-conflicts.yaml"
  pin_reference="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY required to tell this resolver from one in another repository}/.github/workflows/auto-resolve.yaml@"
  pin_reference_re="$(ere_escape "$pin_reference")"
  pin_files=()
  [[ -f "$caller" ]] || return 0
  # A caller pinning SOMEONE ELSE'S resolver names another repository, and this
  # release's sha means nothing there.
  grep -qF "$pin_reference" "$caller" || return 0
  pin_files=("$caller:1")
  # A consumer's README documents its own subject and names no pin to move.
  if [[ -f README.md ]] && grep -qF "$pin_reference" README.md; then
    pin_files+=("README.md:2")
  fi
}

# A count that does not match is a line that changed shape, and the rewrite would
# then no-op and leave a stale sha that nothing reports. Run BEFORE any release
# push: after the tags are published, this refusal leaves a release whose caller
# still runs the previous one, and a re-run re-derives no version to repair it.
check_pin_counts() {
  local entry file expected matched
  for entry in "${pin_files[@]:-}"; do
    [[ -n "$entry" ]] || continue
    file="${entry%:*}"
    expected="${entry##*:}"
    # `grep -c` exits 1 on no match, which under `set -e` would kill the script
    # before the error below could name the file.
    matched=$(grep -cE "${pin_reference_re}[0-9a-f]{40}" "$file" || echo 0) # echo-fallback-ok: a pin that lost its sha is the error the check below reports
    if [[ "$matched" != "$expected" ]]; then
      log "Error: expected $expected resolver pin(s) in $file, found $matched."
      exit 1
    fi
  done
}

# THE PIN FOLLOWS THE TAG. This repository's own caller reaches the resolver by
# SHA, and the called workflow reads its scripts back from that same commit
# (`job.workflow_sha`), so a pin left behind runs the previous release's
# resolver — merged code that is inert in production, reported by nothing. The
# README documents the same line for a reader to copy, so it moves with the
# caller and a copied line names the current release. The pin cannot name its
# own commit, so it moves in the commit AFTER the release. It carries the
# `chore(release):` subject the guard further down already skips, so the next
# run does not cut a version whose only content is these lines.
advance_release_pins() {
  local version="$1" sha="$2" entry
  ((${#pin_files[@]})) || return 0
  for entry in "${pin_files[@]}"; do
    sed -i -E "s|(${pin_reference_re})[0-9a-f]{40}.*|\1${sha} # v${version}|" "${entry%:*}"
    git add "${entry%:*}"
  done
  if git diff --cached --quiet; then
    log "The pins already name $sha."
    return
  fi
  git commit -m "chore(release): pin the caller and README at v$version [skip ci]"
  git push origin HEAD
  log "Advanced the release pins to $sha (v$version)."
}

if [[ -n "$LAST_TAG" ]]; then
  if [[ "$(git rev-list -1 "$LAST_TAG")" = "$HEAD_SHA" ]]; then
    repair_major_tag
    # The pin commit follows the release, so its own push can be rejected on its
    # own and leave the release published with the caller on the previous one.
    # This exit is where the next run reaches that state, so it finishes the job
    # rather than reporting nothing to do.
    if [[ "$LIVE" == "false" ]]; then
      configure_git_identity
      collect_pin_files
      check_pin_counts
      advance_release_pins "${LAST_TAG#v}" "$HEAD_SHA"
    fi
    log "HEAD is already $LAST_TAG. No new version to release."
    exit 0
  fi
  CURRENT_VERSION="${LAST_TAG#v}"
  RANGE="$LAST_TAG..HEAD"
else
  CURRENT_VERSION=""
  RANGE="HEAD"
  log "No vX.Y.Z tag found. Cutting the first release."
fi

# `--no-merges` first, because a merge subject carries no Conventional Commits
# prefix and would drag every release down to a patch. This repository resolves
# conflicts with merge commits, so a range CAN hold nothing else — and then the
# no-merges list is empty while the range still carries real changes. Fall back
# to the full list rather than reading the empty one as "nothing happened".
LOG_FILTER=(--no-merges)
SUBJECTS=$(git log "$RANGE" --pretty=format:%s "${LOG_FILTER[@]}")
if [[ -z "$SUBJECTS" ]]; then
  LOG_FILTER=()
  SUBJECTS=$(git log "$RANGE" --pretty=format:%s)
fi
MESSAGES=$(git log "$RANGE" --pretty=format:%B "${LOG_FILTER[@]}")

if [[ -z "$SUBJECTS" ]]; then
  log "No commits since ${LAST_TAG:-the start of history}. Nothing to release."
  exit 0
fi

# A release-docs commit is this script's own output. Releasing on top of it
# would cut a version whose only content is the previous version's changelog,
# once per push, forever.
if ! grep -qvE '^chore\(release\):' <<<"$SUBJECTS"; then
  # The pin advance sits on top of the release commit, so this — not the
  # already-tagged branch above — is the exit a healthy repository takes.
  repair_major_tag
  log "Only release-docs commits since ${LAST_TAG:-the start of history}. Nothing to release."
  exit 0
fi

if [[ -z "$CURRENT_VERSION" ]]; then
  # The first release is 1.0.0, not 0.1.0: `v1` must name something the moment
  # it first moves, and a 0.x line would leave it pointing at nothing.
  NEW_VERSION="1.0.0"
else
  if ! [[ "$CURRENT_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    log "Error: '$LAST_TAG' is not a plain vX.Y.Z tag. Refusing to guess a bump."
    exit 1
  fi
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

# `version` is what this run DECIDED, so it is written on both paths. `released`
# is a claim about work that has happened, so it is written only after the last
# push succeeds — a consumer gating on it must never see `true` for a run that
# died at the commit or the push.
echo "version=$NEW_VERSION" >>"${GITHUB_OUTPUT:-/dev/null}"

if [[ "$LIVE" != "false" ]]; then
  echo "released=false" >>"${GITHUB_OUTPUT:-/dev/null}"
  log "RELEASE_DRY_RUN is '${LIVE}' (live needs exactly 'false'). No tag was created and nothing was pushed."
  exit 0
fi

configure_git_identity

# Before the first git write, so a pin that changed shape refuses a release that
# has not happened yet.
collect_pin_files
check_pin_counts

RELEASE_DATE=$(date -u +%Y-%m-%d)
CHANGELOG_SECTION=$(git log "$RANGE" --pretty=format:"- %s" "${LOG_FILTER[@]}" | awk 'NR <= 40')

NEW_VERSION="$NEW_VERSION" RELEASE_DATE="$RELEASE_DATE" CHANGELOG_SECTION="$CHANGELOG_SECTION" \
  node "$SCRIPT_DIR/promote-changelog.mjs"

git add CHANGELOG.md
if ! git diff --cached --quiet; then
  git commit -m "chore(release): v$NEW_VERSION [skip ci]"
fi

git tag "v$NEW_VERSION"

# --atomic, and the branch BEFORE the major tag. A non-atomic push can land the
# version tag while the branch ref is rejected (main moved under us, or the
# ruleset refused the [skip ci] commit). The tag then names a commit no branch
# reaches, so the next run's `git describe` — which walks reachability — never
# sees it, re-derives the same version, and dies on "tag already exists" every
# time until a human deletes the remote tag.
git push --atomic origin HEAD "refs/tags/v$NEW_VERSION"

# Only once the release itself has landed. `--force` because this tag is DEFINED
# as moving; the vX.Y.Z tag above is never re-pointed.
git tag --force "$MAJOR_TAG"
git push origin --force "refs/tags/$MAJOR_TAG"

echo "released=true" >>"${GITHUB_OUTPUT:-/dev/null}"
head_sha="$(git rev-parse HEAD)"
log "Pushed v$NEW_VERSION and moved $MAJOR_TAG to $head_sha."

advance_release_pins "$NEW_VERSION" "$head_sha"
