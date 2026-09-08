#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body that no test runs: it reads the runner's context
#   — GITHUB_*, a job-scoped GH_TOKEN, an actions/ working directory — or provisions the
#   runner itself, so it has no entry point off a runner.
# Auto-resolve merge conflicts — HANDOFF step. Runs when PREPARE found conflicted
# paths no later stage of this workflow can resolve, and comments + fails loud
# BEFORE any LLM cost is spent.
#
# TWO refusals reach here, and they need different sentences (agent-glovebox#6104):
#
#   - A path the CALLER reserves. Its own tooling declares the file hand-resolved
#     and gives the reason, which this comment quotes. Such a file merges
#     textually and no lockfile tool re-derives it, so the `.gitattributes`
#     verdict, the lock-command remedy and the blocked label are all false of it.
#     A later conflict on the same PR is still the resolver's, so no label. The
#     per-head attempt mark bounds the re-run instead, for its own TTL and floor
#     (AUTO_RESOLVE_ATTEMPT_TTL_HOURS, AUTO_RESOLVE_ATTEMPT_FLOOR_MINUTES) — this
#     step writes no handoff mark, so a base that moves past the floor re-takes the
#     head, at the cost of a job and no model spend, until the head ages out.
#   - A path with no textual resolution at all — a binary, or a `-merge` file
#     owned by no resolve-generated rule. (A `-merge` LOCKFILE does not reach
#     here: it IS owned by a rule, so the pre-pass re-derives it by re-running
#     its lock command.) That verdict comes from the PR HEAD's `.gitattributes`,
#     the copy `git merge` itself consulted, so it ends in the blocked label
#     rather than a bare retry, and a later push that changes that file retires
#     it. The comment below says which file to change.
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/resolver/lib/pr-labels.bash
source "$_SCRIPT_DIR/../lib/pr-labels.bash"
# shellcheck source=.github/resolver/lib/step-output.bash
source "$_SCRIPT_DIR/../lib/step-output.bash"
# shellcheck source=.github/resolver/lib-ci-retry.sh
source "$_SCRIPT_DIR/../lib-ci-retry.sh"
# shellcheck source=.github/resolver/lib/pr-status-comment.bash
source "$_SCRIPT_DIR/../lib/pr-status-comment.bash"

: "${PR:?PR required}"
: "${BASE_REF:?BASE_REF required}"
: "${UNRESOLVABLE:?UNRESOLVABLE required}"

# prepare.sh's record of what it refused on the caller's say-so, `path<TAB>reason`.
# The one place either reason is decided, so this step reports the refusal rather
# than diagnosing it a second time. Absent for a caller that declares no
# hand-resolved output, which leaves every path below in the unmergeable half.
declare -A reserved_reason=()
if [[ -n "${HAND_RESOLVED_FILE:-}" && -f "${HAND_RESOLVED_FILE}" ]]; then
  while IFS=$'\t' read -r hr_path hr_reason; do
    [[ -n "$hr_path" && -n "$hr_reason" ]] && reserved_reason["$hr_path"]="$hr_reason"
  done <"${HAND_RESOLVED_FILE}"
fi

read -ra paths <<<"$UNRESOLVABLE"
for f in "${!reserved_reason[@]}"; do
  # `unresolvable` crosses the step boundary whitespace-separated, so a path
  # carrying a space arrives as fragments and matches no reason. Loud, because the
  # fragments then take the unmergeable half's permanent label and its false cause.
  [[ " ${UNRESOLVABLE} " == *" ${f} "* ]] ||
    echo "::warning::the caller reserves '${f}', which this step's path list does not carry whole; its conflict is reported as unmergeable instead."
done
reserved=()
unmergeable=()
for f in "${paths[@]}"; do
  if [[ -n "${reserved_reason["$f"]:-}" ]]; then
    reserved+=("$f")
  else
    unmergeable+=("$f")
  fi
done

body="⚠️ **Cannot auto-resolve the merge conflict with \`${BASE_REF}\`**"
if [[ ${#reserved[@]} -gt 0 ]]; then
  body+=$'\n\nThis repository\'s own tooling reserves these files, so no model may write them:\n\n'
  for f in "${reserved[@]}"; do
    body+="- \`${f}\` — ${reserved_reason["$f"]}"$'\n'
  done
  body+=$'\nResolve them by hand: merge `'"${BASE_REF}"$'` locally, settle each file yourself, and push the merge. Auto-resolve still takes this pull request\'s other conflicts.'
  # Said only where it is TRUE. On a mixed refusal the block below applies the label
  # for its own half, and a sentence four lines above denying that is the defect
  # this whole change is about.
  if [[ ${#unmergeable[@]} -eq 0 ]]; then
    body+=$' No label is applied, so a later conflict on this pull request still reaches the resolver.'
  fi
fi
if [[ ${#unmergeable[@]} -gt 0 ]]; then
  body+=$'\n\nThese files cannot be merged textually (lockfile/binary):\n\n'
  for f in "${unmergeable[@]}"; do
    body+="- \`${f}\`"$'\n'
  done
  body+=$'\nResolve by hand: merge `'"${BASE_REF}"$'` locally and re-run the tool that owns each file (e.g. `pnpm install --lockfile-only` / `uv lock` after merging the manifests), then push the merge commit.\n\nAuto-resolve is now labelled `'"${PR_LABEL_AUTO_RESOLVE_BLOCKED}"$'` on this PR and will skip it. That verdict comes from this branch\'s own `.gitattributes`, which is the copy `git merge` read — a push that lets these paths merge textually retires it; otherwise retrying would only re-spend on the same refusal. Remove the label to re-enable it.'
fi

# The verdict REPLACES this run's "working on it" comment, so the PR carries one
# auto-resolve comment that always states the current answer.
pr_status_comment_set "$PR" "$body"

# Stop later scans from re-spending on the same attribute-derived verdict. Never
# for a reserved path alone: that label is permanent until a human removes it,
# and it would skip every FUTURE conflict on this PR the resolver could land.
if [[ ${#unmergeable[@]} -gt 0 ]]; then
  apply_blocked_label "$PR" "$PR_LABEL_AUTO_RESOLVE_BLOCKED" Auto-resolve
fi

# The verdict this run published, for outcome.py: this conflict is now a human's.
step_output "published=handoff"

if [[ ${#unmergeable[@]} -gt 0 ]]; then
  echo "::error::unmergeable conflict(s) with ${BASE_REF}: ${unmergeable[*]} — no textual resolution exists and no resolve-generated rule owns these paths; a human must re-derive them and push the merge."
fi
if [[ ${#reserved[@]} -gt 0 ]]; then
  echo "::error::hand-resolved conflict(s) with ${BASE_REF}: ${reserved[*]} — this repository declares these outputs hand-resolved, so no model resolves them; a human must settle them and push the merge."
fi
exit 1
