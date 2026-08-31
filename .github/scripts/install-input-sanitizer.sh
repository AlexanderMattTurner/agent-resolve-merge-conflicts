#!/usr/bin/env bash
# Install the agent-input-sanitizer package the PR-review scripts import
# (sanitize-pr-input.mjs, post-merge-delta-review.sh).
# Installs beside THIS script, so ESM resolution from those scripts finds it
# without touching the repository's own package.json or lockfile — repos synced
# from this template need no sanitizer dependency of their own. Beside the
# script rather than in the working directory, because merge-delta-review.yaml
# runs this from a CLONE while the working directory is the caller's checkout:
# a cwd-relative prefix would install into a tree the importer never reads.
# This script is the single source of the pinned version.
set -euo pipefail

SANITIZER_VERSION="1.38.0"

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

npm install --prefix "$here" --no-save --no-package-lock \
  --ignore-scripts --no-audit --no-fund \
  "agent-input-sanitizer@${SANITIZER_VERSION}"
