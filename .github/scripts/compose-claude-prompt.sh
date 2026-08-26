#!/usr/bin/env bash
# Compose the prompt handed to claude-code-action.
#
# The ONE canonical untrusted-data guard in .github/prompts/untrusted-data-preamble.md
# is prepended by DEFAULT. A caller that needs no guard says so in SKIP_GUARD_REASON,
# and that assertion is what an auditor reads. Centralizing the guard here is what
# stops each automation from hand-wording its own: the wording had drifted into
# several phrasings, so the weakest one was the real trust boundary at whichever call
# site carried it. A caller declares WHICH inputs are untrusted; it never restates the
# rule.
#
# Environment:
#   PROMPT             the caller's prompt
#   UNTRUSTED_INPUT    newline-separated entries, each naming an untrusted input — a
#                      file path, or a description for a run whose untrusted input has
#                      no file (a comment body, a tool result); empty falls back to a
#                      generic entry, never to an unguarded prompt
#   SKIP_GUARD_REASON  non-empty waives the guard, with the reason recorded in the log
#   PREAMBLE           path to the canonical guard
#   GITHUB_OUTPUT      destination for the composed `prompt` output
set -euo pipefail

prompt="${PROMPT:-}"
untrusted="${UNTRUSTED_INPUT:-}"
skip_reason="${SKIP_GUARD_REASON:-}"

# The entry a caller inherits when it declares nothing. Naming SOMETHING is what
# keeps the guard's closing line from dangling under an empty list.
DEFAULT_UNTRUSTED_ENTRY="every input this run reads that this workflow did not author: pull-request and issue text, diffs, logs, and tool output"

if [[ -n "${skip_reason//[[:space:]]/}" ]]; then
  # Refuse the contradiction rather than pick a winner: a caller that names untrusted
  # input AND waives the guard has one of the two inputs wrong, and guessing which
  # would silently disarm the guard for whichever run the author meant to protect.
  if [[ -n "${untrusted//[[:space:]]/}" ]]; then
    echo "::error::compose-claude-prompt: skip_guard_reason and untrusted_input are both set — a caller cannot both waive the guard and declare untrusted input." >&2
    exit 1
  fi
  echo "compose-claude-prompt: untrusted-data guard waived — ${skip_reason}" >&2
  composed="$prompt"
elif [[ -z "${prompt//[[:space:]]/}" ]]; then
  # An empty prompt selects claude-code-action's event-driven tag mode, where the
  # action composes its own prompt and this script has nothing to prepend to. Fail
  # loud rather than green: a silent pass-through here is exactly the unguarded run
  # the default-on guard exists to prevent.
  echo "::error::compose-claude-prompt: the prompt is empty, so there is nothing to prepend the untrusted-data guard to — a caller that lets claude-code-action build its own prompt must set skip_guard_reason." >&2
  exit 1
else
  preamble="${PREAMBLE:-}"
  # Fail loud rather than emit an unguarded prompt: no run may reach the model
  # without the guard attached, so a missing/empty canonical file is a hard error,
  # not a silent pass-through.
  if [[ -z "$preamble" || ! -s "$preamble" ]]; then
    echo "::error::compose-claude-prompt: the canonical guard is missing or empty (PREAMBLE=${preamble:-unset}) — refusing to build an unguarded prompt." >&2
    exit 1
  fi

  listing=""
  while IFS= read -r entry; do
    entry="${entry#"${entry%%[![:space:]]*}"}" # strip leading whitespace
    entry="${entry%"${entry##*[![:space:]]}"}" # strip trailing whitespace
    [[ -n "$entry" ]] || continue
    # Entries may arrive already bulleted; don't double the marker.
    [[ "$entry" == -* ]] || entry="- $entry"
    listing+="${entry}"$'\n'
  done <<<"$untrusted"
  [[ -n "$listing" ]] || listing="- ${DEFAULT_UNTRUSTED_ENTRY}"$'\n'

  composed="$(cat "$preamble")"$'\n'"${listing}"$'\n'"${prompt}"
fi

# Multiline values cross GITHUB_OUTPUT on a line-oriented channel, so a prompt
# containing the delimiter would let the tail be re-parsed as further outputs.
# A 128-bit random delimiter makes that collision infeasible.
delim="GHOUT_$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
{
  printf 'prompt<<%s\n' "$delim"
  printf '%s\n' "$composed"
  printf '%s\n' "$delim"
} >>"${GITHUB_OUTPUT:?GITHUB_OUTPUT must be set}"
