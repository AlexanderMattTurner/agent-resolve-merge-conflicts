#!/usr/bin/env bash
# Drop this repository's regenerated files from a raw PR diff, IN PLACE.
#
# claude-review.yaml passes this as the outsourced reviewer's `elide-command`,
# and the reviewer calls it as `<this> <raw-diff>` before it counts or sanitizes
# the diff — so both the line budget and the read see the diff a reviewer will
# actually act on.
#
# The classification is not made here. `resolve-generated.mjs --owned
# --rederived-only` names the paths whose regen rule sets `rederivedByCheck`, and
# `strip-generated-diff.mjs` carries why that flag, and not "is generated", is
# what decides. An empty rule list leaves the diff untouched.
set -euo pipefail

raw_diff="${1:?the raw diff to filter, in place}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
omit_list="$(mktemp)"
filtered="$(mktemp)"
trap 'rm -f "$omit_list" "$filtered"' EXIT

node "${here}/resolve-generated.mjs" --owned --rederived-only >"$omit_list"
node "${here}/strip-generated-diff.mjs" "$omit_list" <"$raw_diff" >"$filtered"
# Copied onto the original rather than moved: the reviewer holds this path, and a
# rename would leave it pointing at a file that no longer exists.
cat "$filtered" >"$raw_diff"
