# shellcheck shell=bash
# kcov-exclude: a GitHub Actions step body that no test runs: it reads the runner's context
#   — GITHUB_*, a job-scoped GH_TOKEN, an actions/ working directory — or provisions the
#   runner itself, so it has no entry point off a runner.
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set shell options.
#
# The auto-resolver's ONE-ATTEMPT-PER-HEAD mark, shared by the two sides that must agree on
# it: the resolve job marks the head commit it ran against, and discover skips any PR whose
# current head carries the mark. It stops a paid re-resolution of the tree that just failed.
#
# A commit STATUS rather than a label or a comment, because the fact recorded is a property
# of one tree and must be keyed by that tree's SHA. A label is PR-scoped and survives a
# branch move; pushing new commits clears a status by construction.
#
# The mark EXPIRES. It is written BEFORE the resolution runs, because the runs worth
# stopping are the ones that end badly and never reach a final step, so it cannot tell "this
# tree was resolved" from "the resolver was broken". discover.py therefore skips a head only
# while its newest mark is younger than AUTO_RESOLVE_ATTEMPT_TTL_HOURS — and, once the mark
# is AUTO_RESOLVE_ATTEMPT_FLOOR_MINUTES old, only while the base has not moved since the mark
# was written. Spend per head stays capped at (commit-age window / floor) attempts while the
# base moves, (window / TTL) while it does not.
#
# A run RELEASES its own mark when it spent NOTHING. Prepare's no-op exits (the base is
# already in the head; the merge would fast-forward the PR away) mean this tree was never
# resolved, so an unreleased mark would suppress every scan for a full TTL. The outer bound
# is the commit-age window in discover.py: an untouched branch leaves the candidate set.

# shellcheck source=.github/resolver/lib/commit-status-mark.bash
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/commit-status-mark.bash"
# shellcheck source=.github/resolver/lib/shared-names.bash
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/shared-names.bash"
# shellcheck source=.github/resolver/lib/run-url.bash
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run-url.bash"

# This file is the WRITER half of the mark. The reader half — the freshness test
# and the AUTO_RESOLVE_ATTEMPT_TTL_HOURS knob it reads — lives in
# auto-resolve/discover.py, the only thing that ever consults a mark. Both halves
# read the context string out of shared-names.json, so neither can rename it
# alone: a reader querying a context nobody writes finds nothing and reports the
# head unmarked, which is the failure the mark exists to prevent.
AUTO_RESOLVE_ATTEMPT_CONTEXT="$(shared_name .commit_status_marks.auto_resolve_attempt)"

# The HANDOFF mark, written when a paid run resolved what it could and left conflict
# markers for a human. discover holds this one with no floor and no TTL: the attempt
# mark expires because the failure it records may have been the resolver's own, while
# this one records the MODEL's verdict on this tree, which a re-run reproduces at full
# cost. A push to the head clears it, because the tree it judged is then gone.
AUTO_RESOLVE_HANDOFF_CONTEXT="$(shared_name .commit_status_marks.auto_resolve_handoff)"

# The DECLINE mark, written when the model read the conflict and left the markers on
# purpose. Split from the handoff mark because the two answer different questions: a
# handoff records that the HARNESS delivered nothing (a denied tool, a shard that wrote
# no file), which a resolver change plausibly repairs, so discover retires it on one. A
# decline records the MODEL's verdict on these hunks, which a resolver change does not
# alter — retiring the two together re-bought one PR's identical refusal three times in
# one day. Only a push to the head clears this one.
AUTO_RESOLVE_DECLINED_CONTEXT="$(shared_name .commit_status_marks.auto_resolve_declined)"

# The longest a resolve run can live. Past it a mark's run has certainly ended,
# whatever the mark itself records, because GitHub cancels the job at its own
# `timeout-minutes`. It is the fallback liveness test for a mark that names no
# run, which is every mark written before marks carried one.
AUTO_RESOLVE_RUN_MAX_SECS="${AUTO_RESOLVE_RUN_MAX_SECS:-4200}"

# auto_resolve_claim_state REPO SHA TTL_SECS — print what the run holding SHA's
# attempt mark is doing now, as one of three words:
#
#   in_flight  the run is queued, waiting or running, so it still owns this head
#   concluded  the run has finished, so its claim outlives it and holds nothing
#   unknown    the reads failed, so this says nothing about the holder
#
# The claim outlives the run because the mark is written before the work: a run
# cancelled or killed before any release step reaches no release, so without this
# question a later run cannot tell a dead claim from a live one.
#
# A mark that names NO run is answered by AGE instead. Reporting those `unknown`
# would stand every head down and red every run for a full TTL on the deploy that
# introduces the url, since no mark written before it carries one.
auto_resolve_claim_state() {
  local holder age url run_id status
  holder="$(commit_status_mark_claim_holder "$1" "$2" "$AUTO_RESOLVE_ATTEMPT_CONTEXT" "$3")" ||
    {
      printf 'unknown\n'
      return 0
    }
  age="${holder%% *}"
  url="${holder#* }"
  # The age arm, for a mark that names no run. It answers about the HOLDING
  # mark, never the oldest one on the head: an older mark a release already
  # cancelled would report an age no run ever lived, and every head would then
  # read as free.
  if [[ ! "$url" =~ /actions/runs/([0-9]+) ]]; then
    if [[ "$age" =~ ^[0-9]+$ ]] && ((age > AUTO_RESOLVE_RUN_MAX_SECS)); then
      printf 'concluded\n'
    else
      printf 'in_flight\n'
    fi
    return 0
  fi
  run_id="${BASH_REMATCH[1]}"
  status="$(retry_stdout gh api "repos/$1/actions/runs/${run_id}" --jq .status)" ||
    {
      printf 'unknown\n'
      return 0
    }
  case "$status" in
  completed) printf 'concluded\n' ;;
  "") printf 'unknown\n' ;;
  *) printf 'in_flight\n' ;;
  esac
}

# auto_resolve_mark_attempt REPO SHA DESCRIPTION — record that an attempt ran
# against SHA.
auto_resolve_mark_attempt() {
  commit_status_mark_set "$1" "$2" "$AUTO_RESOLVE_ATTEMPT_CONTEXT" "$3" "$(auto_resolve_run_url)"
}

# auto_resolve_mark_attempt_strict REPO SHA DESCRIPTION RUN_URL — record the
# attempt, print the id GitHub assigned it, and FAIL when the write did not land.
# For the caller that is about to spend: an unmarked head is one every later scan
# re-buys. RUN_URL names this run, so a later run can ask whether this one is
# still alive before it stands down on the mark.
auto_resolve_mark_attempt_strict() {
  commit_status_mark_set_strict "$1" "$2" "$AUTO_RESOLVE_ATTEMPT_CONTEXT" "$3" "$4"
}

# auto_resolve_owns_attempt REPO SHA TTL_SECS ID — true when the mark ID names is
# the oldest one holding SHA, so this run owns the head and may spend.
auto_resolve_owns_attempt() {
  commit_status_mark_owns_claim "$1" "$2" "$AUTO_RESOLVE_ATTEMPT_CONTEXT" "$3" "$4"
}

# auto_resolve_release_attempt REPO SHA DESCRIPTION — give SHA's attempt back,
# for a run that resolved nothing at all (see the release rationale above).
auto_resolve_release_attempt() {
  commit_status_mark_release "$1" "$2" "$AUTO_RESOLVE_ATTEMPT_CONTEXT" "$3"
}

# auto_resolve_mark_handoff REPO SHA DESCRIPTION — record that a paid run handed
# this tree to a human, so no later scan buys the same verdict again.
auto_resolve_mark_handoff() {
  commit_status_mark_set "$1" "$2" "$AUTO_RESOLVE_HANDOFF_CONTEXT" "$3" "$(auto_resolve_run_url)"
}

# auto_resolve_mark_declined REPO SHA DESCRIPTION — record that the model read this
# tree's conflicts and refused them, a verdict no resolver change re-opens.
auto_resolve_mark_declined() {
  commit_status_mark_set "$1" "$2" "$AUTO_RESOLVE_DECLINED_CONTEXT" "$3" "$(auto_resolve_run_url)"
}
