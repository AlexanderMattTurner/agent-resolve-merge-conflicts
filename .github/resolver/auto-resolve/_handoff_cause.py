"""The refusal CAUSE a handoff mark carries, and what a REPEAT of one buys.

PROBLEM CLASS — an unchanged input re-bought at full price, because the record of
the last refusal says a human is needed and never says what the run ran out of.

One head drew five `auto-resolve/handed-off` marks, one per paid run. Each time
one shard ran past `SHARD_TIMEOUT_SECONDS` on one 40-line hunk of one file. Every
run read the same tree under the same bound, so every run stopped in the same
place, and the sixth attempt was a person (agent-glovebox #5644).

The cause rides the mark's own DESCRIPTION. That keys the record to one tree by
construction: a push to the head clears every status on it, so a count taken here
can only ever count runs against the SAME tree.

Two answers follow a repeat, in order:

* the first repeat ESCALATES. The fan-out multiplies its per-shard bound by
  `SHARD_TIMEOUT_ESCALATION`, so the hunk that overran gets a window it never
  had. The fan-out's own budget still caps each shard, so this spends the same
  wall clock on one wave instead of two, and never lengthens the job.
* the refusal after that one writes `auto-resolve/declined` rather than a
  handoff. discover retires a handoff when the resolver's own code changes and
  holds a decline through one, and the escalated run is the evidence that a
  bigger window is not what this conflict needs.

Every read here FAILS OPEN. An unreadable status list answers "no prior cause",
which is what this file's absence used to mean: one more plain handoff.
"""

import json
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

_SHARED_NAMES = json.loads(
    (Path(__file__).resolve().parent.parent / "lib" / "shared-names.json").read_text(
        encoding="utf-8"
    )
)
# The context the handoff marks this module counts are posted under.
# `lib/auto-resolve-attempt.bash` writes them by reading this same file with
# `jq`, so a rename reaches the writer and this reader at once — and a reader
# counting a context nobody writes finds nothing and reports every refusal as a
# first one, which is silent.
HANDOFF_CONTEXT = _SHARED_NAMES["commit_status_marks"]["auto_resolve_handoff"]

# One shard spent its whole `SHARD_TIMEOUT_SECONDS` on a single hunk. A longer
# per-shard window is the one thing that changes this, so it is escalatable.
SHARD_TIMEOUT = "shard-timeout"
# The fan-out as a whole ran out of `FANOUT_BUDGET_SECONDS`. A longer per-shard
# window makes this WORSE, so the cause is recorded and never escalated: what
# changes it is a wider fan-out, which is a change to the resolver, and discover
# already retires a handoff mark on one.
FANOUT_BUDGET = "fanout-budget"
KNOWN_CAUSES = frozenset({SHARD_TIMEOUT, FANOUT_BUDGET})
ESCALATABLE_CAUSES = frozenset({SHARD_TIMEOUT})

# How many escalatable-cause handoffs one head may draw before its next refusal
# is recorded as a decline. One: the first run establishes the cause and the
# second runs escalated, so a third would buy an answer two runs already gave.
ESCALATIONS = 1
# What the escalated run multiplies its per-shard bound by. Two, because the
# fan-out's default budget is twice its default per-shard bound — so the starved
# hunk gets the whole window and the run still finishes inside the same job.
SHARD_TIMEOUT_ESCALATION = 2

# How the cause sits inside a description, and the pattern that reads it back.
# One owner for both directions: a writer and a reader that spell this
# separately agree until the day one of them is edited.
_CAUSE_PREFIX = "cause="
_CAUSE_RE = re.compile(rf"\[{re.escape(_CAUSE_PREFIX)}([a-z0-9-]+)\]")

# How long the status read may take before this answers "no prior cause". The
# call sits in front of a paid fan-out, so a hung API must cost the run seconds
# rather than its budget.
_READ_SECONDS = 30.0


def suffix(cause: str) -> str:
    """The text a mark's description carries to record CAUSE, or "" for none.

    A commit status description is capped at 140 characters and GitHub rejects
    the whole write past it, so an unrecognised cause records nothing rather than
    risking the mark itself. The known causes are short by construction, and the
    two descriptions in `mark-handoff.sh` leave room for one."""
    return f" [{_CAUSE_PREFIX}{cause}]" if cause in KNOWN_CAUSES else ""


def causes_in(statuses: object, context: str) -> list[str]:
    """Every cause recorded by a CONTEXT status in STATUSES, one entry per mark.

    A list rather than a set: the COUNT is the whole signal, so two marks naming
    the same cause must not collapse into one. A status carrying no cause is a
    mark written before this module existed, or a refusal that named none, and
    contributes nothing either way."""
    found: list[str] = []
    if not isinstance(statuses, list):
        return found
    for status in statuses:
        if not isinstance(status, dict) or status.get("context") != context:
            continue
        match = _CAUSE_RE.search(str(status.get("description") or ""))
        if match:
            found.append(match.group(1))
    return found


def escalation_for(causes: tuple[str, ...]) -> int:
    """The multiplier this run's per-shard bound takes, given the causes this
    head's earlier handoffs recorded.

    Any escalatable cause is enough. A head that drew two of them is already past
    its escalation and takes the decline below instead, so raising the multiplier
    with the count would only lengthen a run that is about to be refused."""
    return (
        SHARD_TIMEOUT_ESCALATION
        if any(cause in ESCALATABLE_CAUSES for cause in causes)
        else 1
    )


def escalation_is_spent(causes: tuple[str, ...], cause: str) -> bool:
    """Whether a refusal for CAUSE now should be recorded as a DECLINE.

    The test is on the ESCALATED RUNS this head has already drawn, not on repeats
    of CAUSE itself. The escalated run gives every shard the longer window, so it
    can stop on the fan-out's whole budget where the run before it stopped on one
    shard — and counting per cause would then read two clock refusals as two
    first refusals and escalate the head forever.

    A head that has drawn no escalatable cause at all reaches no decline: nothing
    ran differently, so calling its repeat a settled verdict would strand it on
    evidence the resolver never gathered."""
    escalated = sum(1 for prior in causes if prior in ESCALATABLE_CAUSES)
    return cause in KNOWN_CAUSES and escalated >= ESCALATIONS


def _read_head_statuses() -> object:
    """The commit statuses on the head this run resolved, or None on a FAILED read.

    None and not `[]`: both answer "no prior cause" here, and keeping them apart
    is what lets the warning below name a read that failed rather than a head
    that is genuinely clean. A caller that names no repository or head is neither
    — there is no head to ask about, so nothing was read and nothing failed, and
    the empty list is the honest answer.
    """
    repo = os.environ.get("GH_REPO", "")
    sha = os.environ.get("HEAD_SHA", "")
    if not repo or not sha:
        return []
    try:
        done = subprocess.run(  # noqa: S603
            [
                "gh",
                "api",
                f"repos/{repo}/commits/{sha}/statuses?per_page=100",
                "--paginate",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_READ_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        return None


@lru_cache(maxsize=1)
def head_handoff_causes() -> tuple[str, ...]:
    """The causes this head's earlier handoff marks recorded.

    Cached for the process: the fan-out asks it once to size its shards and the
    refusal path asks it again to decide the mark, and the answer cannot change
    between the two — a status this run posted is not one of THIS run's priors."""
    statuses = _read_head_statuses()
    if statuses is None:
        print(
            "::warning::could not read this head's earlier auto-resolve marks, so "
            "this run treats its refusal as the first one. A repeat is then bought "
            "again rather than escalated."
        )
        return ()
    return tuple(causes_in(statuses, HANDOFF_CONTEXT))


def escalated_shard_timeout(base: int) -> int:
    """BASE, the caller's own per-shard bound, doubled once for a head whose last
    refusal was a shard that ran out of it.

    A multiplier on the caller's number rather than a second number to tune, so a
    caller that raised `SHARD_TIMEOUT_SECONDS` keeps its ratio. It never lengthens
    the job: `fanout.shard_window` still cuts every shard down to the fan-out's
    remaining budget, so this spends that budget on one long wave instead of two
    short ones."""
    factor = escalation_for(head_handoff_causes())
    if factor > 1:
        print(
            f"::notice::this head already handed off on a shard that ran out of "
            f"clock, so each shard gets {base * factor}s here instead of {base}s. "
            "The next refusal on it is recorded as a decline rather than bought "
            "a third time."
        )
    return base * factor


def mark_should_decline(cause: str) -> bool:
    """Whether a refusal for CAUSE takes the DECLINE mark, read off the live head."""
    return escalation_is_spent(head_handoff_causes(), cause)
