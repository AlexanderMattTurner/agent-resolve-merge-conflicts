# shellcheck shell=bash
# kcov-exclude: a GitHub Actions step body that no test runs: it reads the runner's context
#   — GITHUB_*, a job-scoped GH_TOKEN, an actions/ working directory — or provisions the
#   runner itself, so it has no entry point off a runner.
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set
# shell options. Callers provide `retry`/`retry_stdout` (lib-ci-retry.sh).
#
# A per-head mark as a commit STATUS, shared by every sweep that must do a thing
# at most once per head per time window (the auto-resolver's attempt mark, the
# auto-merge re-arm latch). A status attaches to the commit, so pushing new
# commits clears it by construction — nothing to clean up; the TTL bounds
# retries against an UNCHANGED head, so a transient failure self-heals after
# the window instead of stranding the head forever.
#
# A mark can also be RELEASED before its TTL, for the case the TTL is too blunt
# for: the marked run turned out to do nothing at all, so it consumed a budget it
# never spent. The release is a status of its own on a `-released` context rather
# than a non-success state on the mark's context, because a red status on a PR
# head is not inert — GitHub reports the head UNSTABLE, which is the input the
# auto-merge and re-arm paths read.

if [[ -z "${_COMMIT_STATUS_MARK_SOURCED:-}" ]]; then
  _COMMIT_STATUS_MARK_SOURCED=1

  # shellcheck source=.github/resolver/lib/shared-names.bash
  source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/shared-names.bash"

  # The one spelling of the release convention, read by the writer below, by the
  # freshness reader above it, by auto-resolve/discover.py and by the Python
  # sweeps. Spelling it twice is silent in production: a release posted to a
  # context nothing consults is indistinguishable from no release at all until a
  # PR sits out a TTL nobody can explain.
  _COMMIT_STATUS_RELEASED_SUFFIX="$(shared_name .commit_status_marks.released_suffix)"
  readonly _COMMIT_STATUS_RELEASED_SUFFIX
  _commit_status_mark_released_context() {
    printf '%s%s' "$1" "$_COMMIT_STATUS_RELEASED_SUFFIX"
  }

  # _commit_status_mark_statuses REPO SHA — print every status on SHA as ONE JSON
  # array, every page read. PROBLEM CLASS — a status reader that sees only the
  # first page: GitHub serves 30 statuses a page by default, so on a busy head
  # the mark, its release or the oldest claim sits on a later page and a reader
  # of one page answers about statuses it never saw. Every reader below takes
  # this list; none calls `gh api` on its own. Returns non-zero when the read
  # failed, and each caller says which way that failure answers.
  _commit_status_mark_statuses() {
    local pages
    pages="$(retry_stdout gh api --paginate --slurp "repos/$1/commits/$2/statuses?per_page=100")" || return 1
    jq -c 'add // []' <<<"$pages"
  }

  # _commit_status_mark_query REPO SHA CONTEXT TTL_SECS FILTER — run the jq FILTER
  # over SHA's statuses. FILTER may call `holding`: the CONTEXT marks younger than
  # TTL_SECS that postdate every release, which is the one definition of a mark
  # that still HOLDS the head. A release stamped in the same second as a mark wins.
  _commit_status_mark_query() {
    local statuses
    statuses="$(_commit_status_mark_statuses "$1" "$2")" || return 1
    jq -r --arg context "$3" --arg released_context "$(_commit_status_mark_released_context "$3")" \
      --argjson ttl "$4" '
      def at: .created_at | fromdateiso8601;
      def holding:
        ([.[] | select(.context == $released_context) | at] | max // 0) as $released
        | [.[] | select(.context == $context) | select(at > (now - $ttl)) | select(at > $released)];
      '"$5" <<<"$statuses"
  }

  # commit_status_mark_fresh REPO SHA CONTEXT TTL_SECS — true when SHA carries a
  # CONTEXT status younger than TTL_SECS that no later release cancels. The
  # comparison runs inside jq (`now`, `fromdateiso8601`) rather than through
  # `date -d`, which is GNU-only.
  #
  # A query that fails (a deleted commit, an API blip surviving the retries)
  # answers "not fresh": the cost of a redundant action is one run, while
  # wrongly reporting "fresh" would silently skip a head the sweep should handle.
  # A release stamped in the same second as the mark it cancels wins for the same
  # reason — the failure it exists to prevent is a head nothing ever retries.
  commit_status_mark_fresh() {
    if (($# != 4)); then
      echo "commit_status_mark_fresh: usage: commit_status_mark_fresh REPO SHA CONTEXT TTL_SECS" >&2
      return 2
    fi
    local fresh
    fresh="$(_commit_status_mark_query "$@" 'holding | length > 0')" || fresh=false
    [[ "${fresh:-false}" == "true" ]]
  }

  # commit_status_mark_set_strict REPO SHA CONTEXT DESCRIPTION [TARGET_URL] —
  # record the mark, print the id GitHub assigned it, and return non-zero when
  # the write failed.
  #
  # For the caller whose NEXT step spends money. An unmarkable head is one every
  # later scan re-buys at full cost, so that caller has to see the failure — which
  # the best-effort writer below cannot show it. The id is what settles a race
  # between two runs that both marked the same head: see
  # commit_status_mark_owns_claim.
  #
  # TARGET_URL names the RUN that holds the mark, which is what lets a later
  # reader ask whether that run is still alive. Without it a run that died
  # between the mark and its release holds the head until the mark's TTL, and no
  # reader can tell that from a run still working.
  commit_status_mark_set_strict() {
    if (($# < 4 || $# > 5)); then
      echo "commit_status_mark_set_strict: usage: commit_status_mark_set_strict REPO SHA CONTEXT DESCRIPTION [TARGET_URL]" >&2
      return 2
    fi
    # The url is appended only when the caller has one: GitHub rejects an empty
    # `target_url`, so passing the flag unconditionally would fail every write
    # from a caller that names no run.
    local extra=()
    if [[ -n "${5:-}" ]]; then
      extra=(-f "target_url=$5")
    fi
    retry_stdout gh api --method POST "repos/$1/statuses/$2" \
      -f "state=success" \
      -f "context=$3" \
      -f "description=$4" \
      ${extra[@]+"${extra[@]}"} \
      --jq .id
  }

  # commit_status_mark_claim_holder REPO SHA CONTEXT TTL_SECS — print, for the
  # mark that HOLDS SHA, its age in whole seconds and the `target_url` it
  # recorded, separated by one space. The holder is the oldest CONTEXT status
  # younger than TTL_SECS that postdates every release — the same mark
  # commit_status_mark_owns_claim arbitrates on, by the same id.
  #
  # A holder that recorded no url prints its age and nothing else, because the
  # AGE is what a caller falls back on: a mark written before this context
  # carried a url has no other liveness evidence. Prints nothing and returns 1
  # only when no mark holds the head or the read failed, which the caller must
  # not read as a dead holder.
  commit_status_mark_claim_holder() {
    if (($# != 4)); then
      echo "commit_status_mark_claim_holder: usage: commit_status_mark_claim_holder REPO SHA CONTEXT TTL_SECS" >&2
      return 2
    fi
    local found
    found="$(_commit_status_mark_query "$@" '
      holding | min_by(.id)
      | if . == null then empty else "\(((now - at) | floor)) \(.target_url // "")" end')" || return 1
    [[ -n "$found" ]] || return 1
    printf '%s\n' "$found"
  }

  # commit_status_mark_set REPO SHA CONTEXT DESCRIPTION [TARGET_URL] — record the
  # mark, for a caller whose action already happened. Best-effort: failing to
  # mark must not fail an action that is done, and the worst case is one repeated
  # action next window. A failure still says so, as a warning annotation.
  commit_status_mark_set() {
    if (($# < 4 || $# > 5)); then
      echo "commit_status_mark_set: usage: commit_status_mark_set REPO SHA CONTEXT DESCRIPTION [TARGET_URL]" >&2
      return 2
    fi
    if ! commit_status_mark_set_strict "$@" >/dev/null; then
      echo "::warning::could not record the '$3' status on $2, so the next window may repeat this action."
    fi
  }

  # commit_status_mark_owns_claim REPO SHA CONTEXT TTL_SECS ID — true when ID is
  # the OLDEST CONTEXT mark on SHA that is younger than TTL_SECS and postdates
  # every release.
  #
  # INVARIANT — this is what stops two runs paying for one tree. Reading the mark
  # before writing it cannot do that: two runs that both read before either wrote
  # both saw an unmarked head. GitHub offers no conditional status create, so the
  # claim is settled AFTER the write instead, on the ids GitHub assigned — the
  # list is append-only and the ids increase, so exactly one racing run holds the
  # lowest one and every other stands down having spent nothing.
  #
  # Two directions are deliberate. An unreadable answer OWNS the claim, because a
  # read blip that stranded a conflicted PR costs more than the one duplicate
  # resolve it saves. And a mark the newest release cancels is excluded, or a run
  # whose own earlier mark was released would stand down against itself and park
  # the head for a full TTL.
  commit_status_mark_owns_claim() {
    if (($# != 5)); then
      echo "commit_status_mark_owns_claim: usage: commit_status_mark_owns_claim REPO SHA CONTEXT TTL_SECS ID" >&2
      return 2
    fi
    local id="$5" oldest
    oldest="$(_commit_status_mark_query "$1" "$2" "$3" "$4" '[holding[] | .id] | min // 0')" || return 0
    [[ "$oldest" =~ ^[0-9]+$ ]] || return 0
    # 0 means the read saw no fresh mark at all, including this run's own: GitHub
    # has not surfaced the write yet, which is no evidence another run owns it.
    ((oldest == 0 || id <= oldest))
  }

  # commit_status_mark_release REPO SHA CONTEXT DESCRIPTION — cancel the mark on
  # SHA so the next sweep may act on it again before the TTL. Returns non-zero
  # when the write failed, so no caller reports a release that did not happen.
  commit_status_mark_release() {
    if (($# != 4)); then
      echo "commit_status_mark_release: usage: commit_status_mark_release REPO SHA CONTEXT DESCRIPTION" >&2
      return 2
    fi
    commit_status_mark_set_strict "$1" "$2" "$(_commit_status_mark_released_context "$3")" "$4" >/dev/null
  }
fi
