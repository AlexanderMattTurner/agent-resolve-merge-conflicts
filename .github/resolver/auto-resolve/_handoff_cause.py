"""The refusal CAUSE a handoff mark carries, and what a REPEAT of one buys.

PROBLEM CLASS — an unchanged input re-bought at full price, because the record of
the last refusal says a human is needed and never says what the run ran out of.

One head drew five `auto-resolve/handed-off` marks, one per paid run. Each time
one shard ran past `SHARD_TIMEOUT_SECONDS` on one 40-line hunk of one file. Every
run read the same tree under the same bound, so every run stopped in the same
place, and the sixth attempt was a person (agent-glovebox #5644).

The cause rides the mark's own DESCRIPTION. That keys the record to one tree by
construction: a push to the head clears every status on it, so a record read
here can only ever describe runs against the SAME tree.

A repeat of a cause this head already recorded writes `auto-resolve/declined`
rather than a handoff. The tree, the bound and the model are the ones the first
run had, so the second answers what the first answered; discover retires a
handoff when the resolver's own code changes and holds a decline through one,
which is what stops a third run buying it again.

Every read here FAILS OPEN. An unreadable status list answers "no prior cause",
which is what this file's absence used to mean: one more plain handoff.
"""

import json
import os
import re
import subprocess
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

# One shard spent its whole `SHARD_TIMEOUT_SECONDS` on a single hunk.
SHARD_TIMEOUT = "shard-timeout"
# The fan-out as a whole ran out of `FANOUT_BUDGET_SECONDS`.
FANOUT_BUDGET = "fanout-budget"
# What changes either one is a change to the RESOLVER — a wider fan-out, a
# smaller shard — and discover already retires a handoff mark on one. So a
# repeat under the unchanged resolver has nothing new to read, and the second
# sighting of a cause is a settled answer rather than a run worth buying.
KNOWN_CAUSES = frozenset({SHARD_TIMEOUT, FANOUT_BUDGET})

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

    A status carrying no cause is a mark written before this module existed, or a
    refusal that named none, and contributes nothing either way."""
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


def cause_is_settled(causes: tuple[str, ...], cause: str) -> bool:
    """Whether a refusal for CAUSE now should be recorded as a DECLINE.

    The test is CAUSE itself, not any earlier refusal: two causes are two
    different things the run ran out of, and the second one is the first time
    anything learned about that one. A head that alternates between them still
    declines on each one's own second sighting, because the mark is per cause."""
    return cause in KNOWN_CAUSES and cause in causes


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


def head_handoff_causes() -> tuple[str, ...]:
    """The causes this head's earlier handoff marks recorded."""
    statuses = _read_head_statuses()
    if statuses is None:
        print(
            "::warning::could not read this head's earlier auto-resolve marks, so "
            "this run treats its refusal as the first one. A repeat is then bought "
            "again rather than declined."
        )
        return ()
    return tuple(causes_in(statuses, HANDOFF_CONTEXT))


def mark_should_decline(cause: str) -> bool:
    """Whether a refusal for CAUSE takes the DECLINE mark, read off the live head."""
    return cause_is_settled(head_handoff_causes(), cause)
