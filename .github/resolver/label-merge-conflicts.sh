#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite: the suite runs the real
#   script as `bash <script>` against stubbed CLIs on PATH, so no run is ever traced.
# Keep the `merge-conflict` label on every open PR whose GitHub-computed
# mergeability is CONFLICTING; clear it once the PR merges cleanly. No push.
#
# Scope: with PR_NUMBER set (a PR event) it syncs that one PR; unset (a base
# push / schedule) it scans every open PR — the only full conflict scan there is.
#
# GitHub answers lazily, and both stale answers are handled here. UNKNOWN means
# no verdict yet; the query enqueues one, and a PR still UNKNOWN after
# MAX_PASSES falls to the git-merge-tree probe below (a scan) or is warned
# about (a PR event). The silent one: right after a base push GitHub serves the
# verdict it computed against the OLD base, a confident MERGEABLE for a PR that
# push just broke. Each row names the tip its verdict used, so a push scan reads
# any tip but the base branch's live one as unresolved (STALE_BASE).
#
# Outputs: `needs-resolver` names the PRs the auto-resolver should take;
# `evict-queue` names the ones holding a merge-queue entry the queue can never
# build, for evict-queue-entries.sh in the privileged job beside this one.
#
# Env: GH_TOKEN, REPO; PR_NUMBER scopes to one PR; BASE_SHA (every full scan)
# turns the staleness check on; MAX_PASSES caps passes; RETRY_DELAY_SECS the
# between-pass wait; SWEEP_PR_LIMIT (lib/pr-sweep.bash) the full-scan listing;
# CONSENT_LABELED=true (a consent-label event) dispatches an already-labeled
# conflict; MERGE_CONFLICT_PROBE overrides the probe path — its own header says why.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/resolver/lib-ci-retry.sh
source "$SCRIPT_DIR/lib-ci-retry.sh"
# shellcheck source=.github/resolver/lib/pr-labels.bash
source "$SCRIPT_DIR/lib/pr-labels.bash"
# shellcheck source=.github/resolver/lib/pr-sweep.bash
source "$SCRIPT_DIR/lib/pr-sweep.bash"
# shellcheck source=.github/resolver/lib-marker-comment.sh
source "$SCRIPT_DIR/lib-marker-comment.sh"

# Exported because the jq programs below read them as `env.` lookups.
export LABEL="$PR_LABEL_MERGE_CONFLICT"
export BLOCKED_LABEL="$PR_LABEL_AUTO_RESOLVE_BLOCKED"
export BASE_GONE_LABEL="$PR_LABEL_BASE_GONE"

MERGE_CONFLICT_PROBE="${MERGE_CONFLICT_PROBE:-$SCRIPT_DIR/merge-conflict-probe.py}"
# Bound here so an unset value stops the run, rather than reaching gh as an
# empty --repo and scanning nothing under a green exit.
REPO="${REPO:?REPO is required}"

# retry on every gh call: a transient GitHub API 5xx must not red the labeler.
retry gh label create "$LABEL" --repo "$REPO" --color d93f0b --force \
  --description "This PR has merge conflicts with its base branch"
retry gh label create "$BASE_GONE_LABEL" --repo "$REPO" --color b60205 --force \
  --description "This PR's base branch no longer exists on the remote"

# Accumulated across passes, unlike `unknown` (a per-pass verdict that each pass
# recomputes): a PR whose mergeability only resolves to CONFLICTING on pass 3 is
# labeled on pass 3, and that transition must still reach the dispatch below.
needs_resolver=""

# The merge-queue entries a settled conflict leaves unbuildable. Named, not
# dropped: `dequeuePullRequest` needs `contents: write`, and this script also
# runs on `pull_request_target`, where that scope would hand a fork event a
# token that can push to the default branch.
evict_queue=""

BASE_GONE_MARKER='<!-- base-branch-gone -->'

# clear_base_gone_notice NUM — delete the base-gone sticky and its label once any
# other verdict settles the PR. A retarget, and a base branch someone recreates
# under the same name, both make a base-less PR MERGEABLE or CONFLICTING again;
# nothing else reads this marker, so without this the comment keeps telling every
# later reader the PR cannot merge.
#
# Gated on the LABEL, which the listing already carries. Reading the comments of
# every open PR in a full scan would spend the shared REST budget the resolver
# dispatch needs; a label costs nothing extra and names the small set that can
# hold a stale notice.
clear_base_gone_notice() {
  local num="$1" base_gone_labeled="$2"
  [[ "$base_gone_labeled" == "true" ]] || return 0
  retry gh pr edit "$num" --repo "$REPO" --remove-label "$BASE_GONE_LABEL"
  # A listing that could not be READ is not "no comment" — but here the wrong
  # answer only leaves a stale sticky one scan longer, so it defers rather than
  # failing the scan for every other PR in it.
  delete_marker_comments "$REPO" "$num" "$BASE_GONE_MARKER" || return 0
}

declare -A verdict_logged=()

# log_verdict NUM STATE ARM ACTION — one line per PR per scan, naming the verdict,
# the arm that produced it and what the label did.
#
# Without it a scan prints nothing per pull request, so a PR the script leaves
# unlabelled is invisible until a human investigates by hand. Every pass re-reads
# every row, so the guard here is what keeps a three-pass scan to one line per
# pull request instead of three.
log_verdict() {
  local num="$1"
  [[ -z "${verdict_logged[$num]+set}" ]] || return 0
  verdict_logged["$num"]=1
  echo "label-merge-conflicts: #${num} $2 ($3) -> $4"
}

# apply_verdict NUM STATE LABELED BLOCKED DRAFT HEAD_REF BASE_GONE_LABELED ARM — the
# label edit and dispatch-cap bookkeeping for a settled CONFLICTING/MERGEABLE
# verdict, whichever of the retry loop, the probe fallback or the REST state settled
# it. ARM names that decider for the log. Mutates the shared $needs_resolver
# accumulator above.
apply_verdict() {
  local num="$1" state="$2" labeled="$3" blocked="$4" draft="$5" head_ref="$6"
  local base_gone_labeled="$7" arm="$8" action
  clear_base_gone_notice "$num" "$base_gone_labeled"
  case "$state" in # case-default-ok: both callers already restrict STATE to CONFLICTING or MERGEABLE before calling
  CONFLICTING)
    action="already labelled"
    if [[ "$labeled" != "true" ]]; then
      retry gh pr edit "$num" --repo "$REPO" --add-label "$LABEL"
      action=labelled
    fi
    log_verdict "$num" "$state" "$arm" "$action"
    # This refusal is what stops a stale verdict from dequeuing a healthy PR.
    # Only a full scan sets BASE_SHA, and there both routes into this arm are
    # current: the staleness gate proved the listing's verdict was computed
    # against the tip the base carries now, and the probe recomputed its own.
    # A label is reversible on the next scan; a dequeue is not.
    if [[ -n "${BASE_SHA:-}" && "$evict_queue " != *" #$num "* ]]; then
      evict_queue="$evict_queue #$num" # once per scan, though passes repeat
    fi
    # A draft opts out only while it is WORK IN PROGRESS: one on a session
    # branch is a draft a ready-PR cap parked, and a parked PR cannot earn its
    # ready slot back while it stays conflicted — so it still dispatches.
    local wip_draft=false
    [[ "$draft" != "true" ]] || is_session_branch "$head_ref" || wip_draft=true
    # GitHub builds no merge ref for a CONFLICTING PR, so these scans are the
    # resolver's only trigger; a blocked PR would hold a dispatch-cap slot
    # forever. A PR event (PR_NUMBER set) dispatches only the FLIP — except a
    # consent-label event, which releases the consent deferral on an ALREADY-
    # labeled conflict. Forks dispatch as usual: the resolver gates maintainer edits.
    if [[ "$wip_draft" != "true" && "$blocked" != "true" ]] &&
      [[ -z "${PR_NUMBER:-}" || "$labeled" == "false" || "${CONSENT_LABELED:-}" == "true" ]] &&
      [[ "$needs_resolver " != *" #$num "* ]]; then # once per scan, though passes repeat
      needs_resolver="$needs_resolver #$num"
    fi
    ;;
  MERGEABLE)
    action="not labelled"
    if [[ "$labeled" != "false" ]]; then
      retry gh pr edit "$num" --repo "$REPO" --remove-label "$LABEL"
      action="label cleared"
    fi
    log_verdict "$num" "$state" "$arm" "$action"
    # The auto-resolver's opt-out is scoped to the conflict that earned it: a PR
    # that merges cleanly again has had that conflict resolved, so leaving the
    # label on would silently exclude the branch from auto-resolve for the rest
    # of its life. Clearing it here is what keeps the block one-conflict-wide.
    [[ "$blocked" == "false" ]] || retry gh pr edit "$num" --repo "$REPO" --remove-label "$BLOCKED_LABEL"
    ;;
  esac
}

# base_gone_notice NUM BASE_REF — keep ONE comment saying the PR's base branch is
# gone. The verdict is terminal and the PR is unmergeable until a human retargets
# or closes it, so it has to reach a human: a ::warning:: reaches only a run log,
# and every later scan re-derives the same answer in silence. Editing one sticky
# is what stops a notice per scan.
base_gone_notice() {
  local num="$1" base_ref="$2" rc=0 body
  body="$(mktemp)"
  {
    printf '%s\n\n' "$BASE_GONE_MARKER"
    # shellcheck disable=SC2016 # the backticks are markdown for the comment, not a substitution
    printf 'This pull request cannot merge: its base branch `%s` no longer exists on the remote.\n\n' "$base_ref"
    printf 'GitHub computes no mergeability against a branch that is gone, and the auto-resolver fails fetching it, so no scan can settle this PR. Retarget it to a live base branch, or close it.\n'
  } >"$body"
  # A listing that could not be READ is not "no comment": treating it as one posts
  # a fresh notice on every broken-token run. Report it and leave the sticky alone.
  post_or_edit_marker_comment "$REPO" "$num" "$BASE_GONE_MARKER" "$body" || rc=$?
  rm -f "$body"
  return "$rc"
}

# Percent-encode a branch name for a URL path. `gh api` takes the endpoint as a
# URL, so a branch named `release#2` would truncate the path at the `#` and read
# a different branch. The slashes go back in afterwards: GitHub's compare
# endpoint takes `feature/x` as a path, and `%2F` is not the same ref to it.
encode_ref() {
  local encoded
  encoded="$(jq -rn --arg s "$1" '$s|@uri')"
  printf '%s' "${encoded//%2F//}"
}

declare -A base_tip_cache=()
# Set the variable `_bt_target` names to the tip `_bt_ref` carries on the remote right
# now, memoized for one pass. Empty when the branch is gone or the read fails; the caller
# leaves such a row unresolved, since it has no tip to judge the verdict by. Assigning by
# name is what makes the memo work: a `$(base_tip ...)` capture runs the call in a
# subshell, so the cache write never reaches this shell and every row spends another read
# of the same ref.
base_tip() {
  local _bt_target="$1" _bt_ref="$2" _bt_tip=""
  if [[ -z "${base_tip_cache[$_bt_ref]+set}" ]]; then
    # A failed read reports through this same output, so drop it rather than
    # cache an error message as a sha.
    if ! _bt_tip="$(gh api "repos/$REPO/git/ref/heads/$_bt_ref" --jq .object.sha)"; then
      _bt_tip=""
    fi
    base_tip_cache["$_bt_ref"]="$_bt_tip"
  fi
  printf -v "$_bt_target" '%s' "${base_tip_cache[$_bt_ref]}"
}

# compare_status HEAD_OID BASE_REF — GitHub's own word for how HEAD_OID sits
# against the tip BASE_REF has right now: `ahead`, `behind`, `identical` or
# `diverged`. Non-zero, printing nothing, when the compare could not be read.
#
# GitHub resolves the branch name inside the compare, so one call answers this
# and reads the tip at the same instant a separate read could already be stale.
# The read is retried, so a transient API fault does not re-label a contained
# head and re-dispatch the resolver for it. Both predicates below fail CLOSED on
# an unreadable answer, which leaves GitHub's own verdict standing.
compare_status() {
  local head_oid="$1" base_ref="$2" encoded
  [[ -n "$head_oid" && -n "$base_ref" ]] || return 1
  encoded="$(encode_ref "$base_ref")"
  retry_stdout gh api "repos/$REPO/compare/${encoded}...${head_oid}" --jq .status
}

# head_contains_base HEAD_OID BASE_REF — whether HEAD_OID already carries the
# tip BASE_REF has right now. Merging such a head into its base is a
# fast-forward, so no conflict is possible and a CONFLICTING verdict about it is
# wrong. GitHub serves one anyway, and keeps serving it: on a stacked chain whose
# parent was merged into the child, every scan labels the child and dispatches a
# resolve that then reports nothing to resolve.
head_contains_base() {
  local status
  status="$(compare_status "$1" "$2")" || return 1
  [[ "$status" == "ahead" || "$status" == "identical" ]]
}

# either_contains_other HEAD_OID BASE_REF — true when one of the two commits is
# already an ancestor of the other, whichever way round.
#
# `behind` counts as much as `ahead`: prepare.sh ends BOTH directions through
# `no_op_exit` — a base contained in the head leaves no merge to make, and a head
# contained in the base fast-forwards to the base, with nothing of the PR's own to
# push. So neither direction can clear a label, and both buy a futile resolver
# dispatch on every scan.
either_contains_other() {
  local status
  status="$(compare_status "$1" "$2")" || return 1
  [[ "$status" == "ahead" || "$status" == "identical" || "$status" == "behind" ]]
}

# dirty_in_rest NUM HEAD_OID BASE_REF — true when REST still calls this pull
# request's merge `dirty`, and neither of the two commits already carries the
# other.
#
# GitHub's conflict verdict outlives the conflict. PR #6415 read `dirty` for 85
# minutes across six scans while `git merge-tree` merged the same two commits
# cleanly, so no arm here labelled it and no resolver ran. Only a push to the head
# makes GitHub recompute, and the resolver's dispatch is what pushes one —
# prepare.sh's clean-merge arm commits the merge and land.sh pushes it — so
# labelling a `dirty` head is what clears the stale verdict.
#
# The containment test is what stops a label nothing can clear: where one of the
# two already carries the other, the merge is a fast-forward in one direction or
# the other, and prepare.sh ends such a run through `no_op_exit` without pushing
# anything.
#
# An unreadable state answers false, like every other doubt in this script. One
# attempt, not the shared retry: a scan reads this once per unresolved PR, and the
# fallback on a fault is today's answer rather than a wrong label.
dirty_in_rest() {
  local num="$1" head_oid="$2" base_ref="$3" state
  state="$(gh api "repos/$REPO/pulls/$num" --jq .mergeable_state)" || return 1
  [[ "$state" == "dirty" ]] || return 1
  ! either_contains_other "$head_oid" "$base_ref"
}

unknown=()                   # PR numbers this pass could not settle
declare -A unknown_reason=() # num -> "UNKNOWN" or "STALE_BASE base=<oid> want=<tip>"
declare -A unknown_row=()    # num -> \x1f-joined labeled/blocked/draft/head_ref/head_oid/base_ref/base_gone
want=""
# retry-loop-ok: each pass re-reads mergeability GitHub computes asynchronously and labels the PRs it resolved — a poll for a value still being computed, not a blip retry lib-ci-retry.sh's single-command wrapper can express
for ((pass = 1; pass <= ${MAX_PASSES:-2}; pass++)); do
  [[ "$pass" == "1" ]] || sleep "${RETRY_DELAY_SECS:-10}"
  base_tip_cache=()
  # Fields join on \x1f, not @tsv's tab: bash treats tab as IFS whitespace, so
  # `read` squashes a run of tabs and an empty middle field (baseRefOid on a
  # base-less scan) shifts every later field left. Captured before use so a
  # failed listing trips set -e — inside the here-string its status is lost, and
  # an unreadable listing would read as "no open PRs": a scan that saw nothing.
  rows="$(pr_sweep_scoped_prs "$REPO" merge-conflict-labeler \
    number,mergeable,labels,isDraft,headRefName,headRefOid,baseRefOid,baseRefName |
    jq -r '[.[] | [.number, .mergeable, any(.labels[]; .name == env.LABEL),
      any(.labels[]; .name == env.BLOCKED_LABEL), (.isDraft // false),
      (.headRefName // ""), (.headRefOid // ""),
      (.baseRefOid // ""), (.baseRefName // ""),
      any(.labels[]; .name == env.BASE_GONE_LABEL)] | map(tostring) |
      join("")] | join("\n")')"
  unknown=()
  unknown_reason=()
  unknown_row=()
  while IFS=$'\x1f' read -r num state labeled blocked draft head_ref head_oid \
    base_oid base_ref base_gone_labeled; do
    [[ -n "$num" ]] || continue
    arm="GraphQL mergeable"
    # baseRefOid is the base tip GitHub computed this verdict against, and a
    # verdict is current when that tip is the one the branch carries NOW — not
    # the one this run was triggered on. A queued full scan starts minutes late,
    # by which time the base has moved. BASE_SHA is set on every full scan and
    # empty on a PR event, which cancel-in-progress supersedes instead.
    if [[ -n "${BASE_SHA:-}" ]]; then
      # No fallback to BASE_SHA: a row whose baseRefOid happens to equal the
      # trigger commit would then be believed on a tip nobody could read.
      base_tip want "$base_ref"
      want="${want:-<unreadable>}"
      # An unresolved row falls to the retry loop, the probe and the after-loop
      # warning like any other, so the stale MERGEABLE is never acted on.
      [[ "$base_oid" == "$want" ]] || state="STALE_BASE"
    fi
    if [[ "$state" == "CONFLICTING" ]] && head_contains_base "$head_oid" "$base_ref"; then
      echo "::notice::#$num's head already contains $base_ref's tip, so the merge is a fast-forward; reading GitHub's CONFLICTING verdict as wrong."
      state="MERGEABLE"
      arm="head already carries ${base_ref}'s tip"
    fi
    case "$state" in
    CONFLICTING | MERGEABLE)
      apply_verdict "$num" "$state" "$labeled" "$blocked" "$draft" "$head_ref" \
        "$base_gone_labeled" "$arm"
      ;;
    *)
      unknown+=("$num")
      if [[ "$state" == "STALE_BASE" ]]; then
        unknown_reason["$num"]="STALE_BASE base=${base_oid:-<empty>} want=$want"
      else
        unknown_reason["$num"]="UNKNOWN"
      fi
      unknown_row["$num"]="$(printf '%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s' \
        "$labeled" "$blocked" "$draft" "$head_ref" "$head_oid" "$base_ref" \
        "$base_gone_labeled")"
      ;;
    esac
  done <<<"$rows"
  ((${#unknown[@]} == 0)) && break
done

probe_failure_note=""
base_gone=""
if ((${#unknown[@]} > 0)) && [[ -z "${PR_NUMBER:-}" ]]; then
  probe_input=""
  for num in "${unknown[@]}"; do
    IFS=$'\x1f' read -r _labeled _blocked _draft _head_ref _head_oid base_ref \
      _base_gone <<<"${unknown_row[$num]}"
    probe_input="${probe_input}${num}"$'\t'"$base_ref"$'\n'
  done
  probe_err_file="$(mktemp)"
  probe_rc=0
  probe_out="$(printf '%s' "$probe_input" |
    python3 "$MERGE_CONFLICT_PROBE" --clone-url "https://github.com/${REPO}.git" \
      2>"$probe_err_file")" || probe_rc=$?
  probe_err="$(cat "$probe_err_file")"
  rm -f "$probe_err_file"
  if ((probe_rc == 0)); then
    declare -A probe_verdict=()
    while IFS=$'\t' read -r pnum pstate; do
      [[ -n "$pnum" ]] || continue
      probe_verdict["$pnum"]="$pstate"
    done <<<"$probe_out"
    remaining=()
    for num in "${unknown[@]}"; do
      probe_said="${probe_verdict[$num]:-none}"
      verdict="$probe_said"
      IFS=$'\x1f' read -r labeled blocked draft head_ref head_oid base_ref \
        base_gone_labeled <<<"${unknown_row[$num]}"
      arm="local merge (GraphQL ${unknown_reason[$num]:-UNKNOWN})"
      # The arm of last resort, for a row no local answer settles as a conflict: a
      # clean local merge, or a probe that reported nothing at all. REST is then the
      # only verdict left, and a `dirty` one blocks the merge button however stale
      # it is, so the label is what starts the push that clears it.
      if [[ "$verdict" != "CONFLICTING" && "$verdict" != "BASE_GONE" ]] &&
        dirty_in_rest "$num" "$head_oid" "$base_ref"; then
        verdict="CONFLICTING"
        arm="REST mergeable_state=dirty; GraphQL ${unknown_reason[$num]:-UNKNOWN}, local merge ${probe_said}"
      fi
      if [[ "$verdict" == "MERGEABLE" || "$verdict" == "CONFLICTING" ]]; then
        apply_verdict "$num" "$verdict" "$labeled" "$blocked" "$draft" "$head_ref" \
          "$base_gone_labeled" "$arm"
      elif [[ "$verdict" == "BASE_GONE" ]]; then
        base_gone_notice "$num" "$base_ref"
        [[ "$base_gone_labeled" == "true" ]] ||
          retry gh pr edit "$num" --repo "$REPO" --add-label "$BASE_GONE_LABEL"
        # A PR labelled CONFLICTING before its base vanished keeps that label
        # forever otherwise, and a later retarget onto another conflicting base
        # then reads `labeled=true` and skips the resolver dispatch.
        [[ "$labeled" == "false" ]] ||
          retry gh pr edit "$num" --repo "$REPO" --remove-label "$LABEL"
        # Settled, not unresolved: the base branch is gone, so no later scan
        # answers this and the retry warning would repeat forever. Never label
        # it CONFLICTING — that dispatches the resolver to merge the same
        # missing ref. Name it instead, for a human to retarget or close.
        base_gone="$base_gone #${num}(base ${base_ref} is gone)"
      else
        remaining+=("$num")
      fi
    done
    unknown=("${remaining[@]}")
  else
    probe_failure_note=" merge-conflict-probe.py could not settle them: ${probe_err//$'\n'/ }"
  fi
fi

if [[ -n "$base_gone" ]]; then
  echo "::warning::base branch gone for$base_gone; retarget or close them — no scan can settle a PR whose base does not exist."
fi

if ((${#unknown[@]} > 0)); then
  display=""
  for num in "${unknown[@]}"; do
    display="$display #$num(${unknown_reason[$num]:-UNKNOWN})"
  done
  echo "::warning::mergeability unresolved for$display after ${MAX_PASSES:-2} passes;$probe_failure_note the next PR event or scheduled run will retry them."
fi

# The PRs this run makes the resolver's business, for the caller's dispatch.
# Written even when empty so a reader of the step output can tell "no PR needs it"
# from "the script died before it could say" — the caller gates on the value.
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  if [[ -n "$needs_resolver" ]]; then
    echo "Needs the auto-resolver:$needs_resolver — dispatching it."
  fi
  echo "needs-resolver=${needs_resolver# }" >>"$GITHUB_OUTPUT"
  echo "evict-queue=${evict_queue# }" >>"$GITHUB_OUTPUT"
fi
