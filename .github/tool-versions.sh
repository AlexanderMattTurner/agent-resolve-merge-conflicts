# shellcheck shell=bash
# shellcheck disable=SC2034  # every value here is consumed by a script that SOURCES this file
# Pinned versions and digests for tools CI downloads directly, rather than
# through a package manager that would do its own integrity check.
#
# Sourced by the install-*.sh scripts beside it. Not executable, and it must stay
# free of side effects: a consumer sources it purely to read these values.
#
# Bump a version and its digest TOGETHER. A version bumped without its digest
# fails the install closed, which is the intended direction — the opposite
# (a digest that no longer describes the pinned artifact) is the one that would
# certify an unreviewed binary.

# pre-commit runs the merged tree's own hooks before the resolver bundles a
# resolution, so pip-install-ci-tools.sh installs it at this pin rather than at
# PyPI's newest. Read by the resolver, which is why it is pinned beside a digest
# it does not need: pre-commit arrives through pip, which verifies its own.
PRE_COMMIT_VERSION=4.6.1

# mergiraf backs the structural pre-pass in .github/resolver/auto-resolve/prepare.sh:
# a syntax-aware merge that resolves the structural subset of a PR's conflicts so
# only genuinely semantic conflicts reach the paid LLM pass.
#
# install-mergiraf.sh downloads the pinned release tarball from Codeberg and
# sha256-verifies it before extracting. The digest HERE is the anchor, not the
# checksum manifest published alongside the release: a manifest fetched from the
# same tag as the artifact is re-published by anyone who can re-tag that release,
# so it proves the download was not corrupted in transit and says nothing about
# the release being the one we reviewed.
# The second digest is the extracted BINARY, and it is what the already-done skip
# checks. Without it the skip would certify the destination on the binary's own
# `--version` output, so a swapped file that prints the right string would be
# skipped past forever — on exactly the persistent hosts setup.sh reaches.
MERGIRAF_VERSION=v0.18.0
MERGIRAF_SHA256_linux_amd64=4de0986ff9155411dd105958b94362056d0055025db75369eddd3ecd25334cd2
MERGIRAF_SHA256_bin_linux_amd64=2bd569954287e6a905ba570d867ecd4aff94e9a96a702dcfa26cdcdaeb40289e

# tla2tools carries TLC, the model checker that runs docs/tla/*.cfg in CI.
# install-tla2tools.sh downloads the pinned release jar and sha256-verifies it
# before any JVM reads it: the digest here is the anchor, not the release page,
# because an unverified download would execute whoever re-tagged the release.
TLA2TOOLS_VERSION=v1.7.4
TLA2TOOLS_SHA256=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
