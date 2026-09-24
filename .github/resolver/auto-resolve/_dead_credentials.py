"""The credentials this run has seen refused for good, in one record every process reads.

PROBLEM CLASS — a dead credential rediscovered once per shard. A revoked OAuth
token or a spent session allowance answers every call the same way for the rest
of the run. Without a shared record every shard of every rung launches `claude` to
learn it again, and a wide conflict set spends the whole fan-out window on dead
rungs before a live one runs (agent-glovebox #7235).

The first shard to see such a refusal marks the credential here; every later
shard on it, in this fan-out or in a later rung's, skips its launch. What each
fan-out spent on a credential that died is recorded too, so the refusal can tell
an outage from a conflict too big for the window.

The record is a JSONL file named by `AUTO_RESOLVE_DEAD_CREDENTIALS`, which the
workflow puts under the job's RUNNER_TEMP. Each line is ONE `os.write` on an `O_APPEND`
descriptor, so concurrent shard threads and sibling processes never interleave.
Unset means no record: nothing is marked and nothing is skipped, which keeps a
test run or a local run from inheriting another run's verdicts.
"""

import hashlib
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _result_fields import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    credential_dead,
    error_text,
    get,
)

RECORD_ENV = "AUTO_RESOLVE_DEAD_CREDENTIALS"

# The API's words are kept for the log line a skipped shard reports, and capped
# so one record line stays a single small write.
_TEXT_LIMIT = 500


def _record() -> Path | None:
    raw = os.environ.get(RECORD_ENV, "")
    return Path(raw) if raw else None


def fingerprint(env: Mapping[str, str]) -> str:
    """Which credential ENV authenticates with, without writing the credential.

    INVARIANT — the record holds a truncated SHA-256 of the token and never the
    token, so a reader of RUNNER_TEMP learns only which shards shared one. Both
    variables go in, because the ladder sets one and empties the other."""
    pair = (
        f"{env.get('CLAUDE_CODE_OAUTH_TOKEN', '')}\0{env.get('ANTHROPIC_API_KEY', '')}"
    )
    return hashlib.sha256(pair.encode("utf-8")).hexdigest()[:16]


def _entries() -> list[dict[str, Any]]:
    """Every complete line of the record. A last line with no newline is a write
    still in flight in another thread, so it is left for the next read."""
    record = _record()
    if record is None or not record.is_file():
        return []
    lines = record.read_text(encoding="utf-8").split("\n")
    return [json.loads(line) for line in lines[:-1] if line]


def _append(record: Path, entry: dict[str, Any]) -> None:
    line = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(record, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)


def refusal(env: Mapping[str, str]) -> dict[str, Any] | None:
    """The refusal that marked ENV's credential dead this run, or None."""
    credential = fingerprint(env)
    return next(
        (
            entry
            for entry in _entries()
            if entry["kind"] == "dead" and entry["credential"] == credential
        ),
        None,
    )


def mark(env: Mapping[str, str], status: Any, text: Any) -> bool:
    """Record ENV's credential as dead, and say whether this call was the first to.

    Two shards of one wave can both get here first; each appends, and a reader
    takes the earliest line, so the duplicate costs one line and nothing else."""
    record = _record()
    if record is None or refusal(env) is not None:
        return False
    _append(
        record,
        {
            "kind": "dead",
            "credential": fingerprint(env),
            "status": status,
            "text": str(text or "")[:_TEXT_LIMIT],
        },
    )
    return True


def mark_if_refused(env: Mapping[str, str], result: Any, who: str) -> None:
    """Mark ENV's credential dead when RESULT, WHO's run on it, is a refusal no
    later call can get past this run (`credential_dead`)."""
    status = get(result, "api_error_status")
    text = error_text(result)
    if credential_dead(status, text) and mark(env, status, text):
        print(
            f"::warning::{who}: the credential was refused with HTTP {status} "
            f"({text}); every later shard on it this run is skipped.",
            file=sys.stderr,
        )


def skipped(refused: dict[str, Any]) -> dict[str, Any]:
    """The result log of a shard never launched because its credential drew
    REFUSED: that refusal, at zero cost, so every reader of the log sees it."""
    return {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "total_cost_usd": 0,
        "num_turns": 0,
        "api_error_status": refused["status"],
        "result": refused["text"],
        "skipped_dead_credential": True,
    }


def record_spent(env: Mapping[str, str], seconds: float) -> None:
    """Record that a fan-out spent SECONDS of the window on ENV's credential,
    when that credential died during the run."""
    record = _record()
    if record is not None and refusal(env) is not None:
        _append(
            record,
            {"kind": "spent", "credential": fingerprint(env), "seconds": seconds},
        )


def seconds_lost() -> float:
    """How much of the fan-out window this run spent on credentials that died."""
    return sum(entry["seconds"] for entry in _entries() if entry["kind"] == "spent")
