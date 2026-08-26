#!/usr/bin/env bash
# Fetch the untrusted PR diff + metadata and run them through the
# agent-input-sanitizer (sanitize-pr-input.mjs) BEFORE the review agent sees
# them. The agent reads only the sanitized files this writes — never the raw
# API response — so an injection payload hidden in it (zero-width control
# text, ANSI escapes, exfil beacons) cannot reach the agent intact.
#
# Generated-file filter: a path whose regen rule sets `rederivedByCheck` is
# stripped from the diff before it is counted or sanitized.
# strip-generated-diff.mjs carries why that flag, and not "is generated", is
# what decides, and why an empty rule list leaves the diff untouched.
#
# Oversized-diff guard: the base-only checkout means diff.txt is the ONLY source
# of the PR's changes — the agent cannot reconstruct them from the trusted base
# tree, so an enormous diff (a mega-merge, a vendored/generated dump) would be
# ingested whole into an Opus read that is slow, costly, and low-signal. Above
# MAX_DIFF_LINES lines this skips the review, emitting oversized=true so the
# caller posts a "please review manually" notice instead of spending the read.
#
# The guard has TWO sources. The line count is the live one, because the REST
# diff media type serves past GitHub's 20000-line cap. A 406 carrying that cap's
# refusal is still handled: it IS an oversized verdict, and it is deterministic,
# so retrying it only spends the backoff ladder to learn the same answer and then
# reds the check the notice exists to replace.
#
# Requires: GH_TOKEN/GH_REPO, node + `pnpm install` done
# (agent-input-sanitizer on the module path). Emits to GITHUB_OUTPUT:
#   oversized=true|false       — whether the review was skipped for size
#   diff_lines=<n>             — the diff's line count (only when oversized)
# Writes into $PR_INPUT_DIR (only when NOT oversized):
#   diff.txt / meta.txt        — sanitized diff and PR metadata
#   sanitizer-report.txt       — what was neutralized (never empty; says so)
# and, only when oversized:
#   oversized-notice.txt       — the human-review notice body for the caller
set -euo pipefail

# shellcheck source=.github/resolver/lib-ci-retry.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../resolver" && pwd)/lib-ci-retry.sh"

: "${PR:?PR number required}"
: "${PR_INPUT_DIR:?PR_INPUT_DIR required}"

MAX_DIFF_LINES="${MAX_DIFF_LINES:-20000}"

mkdir -p "$PR_INPUT_DIR" # bare-mkdir-ok: post-condition verified on the next line
[[ -d "$PR_INPUT_DIR" ]] || {
  echo "PR_INPUT_DIR ($PR_INPUT_DIR) does not exist after mkdir -p" >&2
  exit 1
}

emit_output() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s\n' "$1" >>"$GITHUB_OUTPUT"
  fi
}

# GitHub's own diff-API cap, and the phrase its refusal carries. Recorded from a
# real 406 on this repository: `could not find pull request diff: HTTP 406:
# Sorry, the diff exceeded the maximum number of lines (20000) (https://…)`.
API_DIFF_LINE_CAP=20000
API_OVERSIZE_MARKER="the diff exceeded the maximum number of lines"

# skip_as_oversized REASON LINES — the ONE place the size skip is decided, so
# both sources of the verdict emit the same outputs and the same notice. REASON
# completes "this PR's diff is …".
skip_as_oversized() {
  emit_output "oversized=true"
  emit_output "diff_lines=$2"
  printf '%s\n' \
    "Automated Opus review skipped: this PR's diff is $1. A change this large should get a human review — please review it manually." \
    >"${PR_INPUT_DIR}/oversized-notice.txt"
  echo "diff is $1; skipping review" >&2
  exit 0
}

# Materialize the raw diff OUTSIDE the agent-readable input dir (the review step
# grants the agent read over PR_INPUT_DIR via --add-dir), so only the SANITIZED
# diff.txt ever reaches the reviewer.
raw_diff="$(mktemp)"
fetch_err="$(mktemp)"
fetch_body="$(mktemp)"
review_diff="$(mktemp)"
omit_list="$(mktemp)"
trap 'rm -f "$raw_diff" "$fetch_err" "$fetch_body" "$review_diff" "$omit_list"' EXIT

# curl, not `gh pr diff`: gh answers 406 at exactly API_DIFF_LINE_CAP, which is
# also MAX_DIFF_LINES's default, so the line count could never fire. The REST
# diff media type serves past it (agent-sanitizer#367: 31,204 lines).
# --fail-with-body keeps a refusal's body, where the marker is.
fetch_diff() {
  curl -sS --fail-with-body --retry 0 \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    -H "Accept: application/vnd.github.v3.diff" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API_URL:-https://api.github.com}/repos/${GH_REPO}/pulls/${PR}"
}

# One unretried attempt first, so a size refusal is classified before the backoff
# ladder starts; anything else is a blip and gets the full budget. It writes to a
# FILE because a refusal's body is the thing to read. The retry uses a command
# substitution, so a failing attempt's body never lands in raw_diff.
if fetch_diff >"$fetch_body" 2>"$fetch_err"; then
  raw_diff_content="$(cat "$fetch_body")"
elif grep -qF "$API_OVERSIZE_MARKER" "$fetch_body" "$fetch_err"; then
  skip_as_oversized \
    "over GitHub's own ${API_DIFF_LINE_CAP}-line diff API cap, so the API refused to serve it" \
    "$API_DIFF_LINE_CAP"
else
  cat "$fetch_err" "$fetch_body" >&2
  raw_diff_content="$(retry_stdout fetch_diff)"
fi
printf '%s\n' "$raw_diff_content" >"$raw_diff"

# resolve-generated.mjs owns the decision; nothing classifies a path here. The
# filter must run BEFORE the line count and before sanitize, so both see the diff
# a reviewer will actually read.
node .github/scripts/resolve-generated.mjs --owned --rederived-only >"$omit_list"
node .github/scripts/strip-generated-diff.mjs "$omit_list" \
  <"$raw_diff" >"$review_diff"

diff_lines="$(wc -l <"$review_diff" | tr -d '[:space:]')"
if ((diff_lines > MAX_DIFF_LINES)); then
  skip_as_oversized \
    "${diff_lines} lines, over the ${MAX_DIFF_LINES}-line limit for automated review" \
    "$diff_lines"
fi
emit_output "oversized=false"

sanitize() { node .github/scripts/sanitize-pr-input.mjs; }

sanitize <"$review_diff" >"${PR_INPUT_DIR}/diff.txt" 2>"${PR_INPUT_DIR}/diff.report.txt"
# Capture the metadata JSON with retry_stdout, THEN pipe the clean result into
# the sanitizer — retrying gh directly inside the `| sanitize` pipe is unsafe (a
# failing attempt would stream partial JSON into the sanitizer, and a SIGPIPE if
# it exited early would trip pipefail).
meta_json="$(retry_stdout gh pr view "$PR" --json title,body,author,files)"
printf '%s' "$meta_json" |
  sanitize >"${PR_INPUT_DIR}/meta.txt" 2>"${PR_INPUT_DIR}/meta.report.txt"

report="${PR_INPUT_DIR}/sanitizer-report.txt"
{
  if [[ -s "${PR_INPUT_DIR}/diff.report.txt" ]]; then
    echo "## Diff"
    cat "${PR_INPUT_DIR}/diff.report.txt"
  fi
  if [[ -s "${PR_INPUT_DIR}/meta.report.txt" ]]; then
    echo "## Metadata"
    cat "${PR_INPUT_DIR}/meta.report.txt"
  fi
} >"$report"

if [[ -s "$report" ]]; then
  echo "sanitizer neutralized injection-shaped content; see ${report}" >&2
else
  echo "(sanitizer found no injection-shaped content in the diff or metadata)" >"$report"
fi
