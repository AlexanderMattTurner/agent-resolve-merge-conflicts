"""Reading one shard's `claude` result log, the way the shell's `jq` programs did.

PROBLEM CLASS — a value read out of an execution log must keep the distinctions
the downstream gate acts on: "the shard reported zero" versus "the shard could
not tell", and "an edit tool was denied" versus "the denied set was never
named". jq's own defaulting rules make those distinctions by construction, so
this module reproduces them explicitly rather than letting Python's truthiness
collapse them.

Pure functions over already-parsed JSON, so the caller owns every read of a file.
Standard library only: the job that runs the fan-out checks out
`.github/scripts` sparsely and uses the system `python3`.
"""

import json
from pathlib import Path
from typing import Any


class _Unreadable:
    """A log that is absent, empty, or not JSON — an errored shard, not a result."""


_UNREADABLE = _Unreadable()


def get(result: Any, key: str) -> Any:
    """`result.key` the way jq reads it: a null result answers null for any key,
    and so does an object that lacks it."""
    return result.get(key) if isinstance(result, dict) else None


def alt(value: Any, fallback: Any) -> Any:
    """jq's `//`: null and false both fall through to the alternative."""
    return fallback if value is None or value is False else value


def cost_of(result: Any) -> Any:
    """0 for a result that never arrived, the reported cost when the field is
    there, and None when it is not — the three states the gate keeps apart."""
    if result is None:
        return 0
    return result.get("total_cost_usd", None)


def denial_count(result: Any) -> Any:
    """The count the shard reported, or the length of the denial records when it
    reported only those."""
    return alt(
        get(result, "permission_denials_count"),
        len(alt(get(result, "permission_denials"), [])),
    )


def denied_tools(result: Any) -> Any:
    """The tool NAMES behind those denials, so a downstream reader can tell a
    denied edit (the write path really was closed) from a denied Bash/TodoWrite
    (the resolver could still edit, and something else explains its output). None
    — not [] — when the result carries only a count: an unnamed set is "cannot
    tell", and folding it into "no edit tool was denied" would assert the very
    thing the count could never establish."""
    if isinstance(result, dict) and "permission_denials" in result:
        # One name per record, defaulted individually: a single default over the
        # whole list would turn a shard with zero denials into one "unnamed".
        return [
            alt(get(record, "tool_name"), "unnamed")
            for record in result["permission_denials"]
        ]
    if alt(get(result, "permission_denials_count"), 0) == 0:
        return []
    return None


def one_shared(all_errored: bool, values: list[Any], *, drop_none: bool) -> Any:
    """The single value every errored shard agrees on, or None. `drop_none`
    matches the two jq programs: the status set keeps its nulls, so one shard
    without a status makes the set disagree, while the text set drops them."""
    unique = {json.dumps(value, sort_keys=True) for value in values}
    if drop_none:
        unique.discard("null")
    if all_errored and len(unique) == 1:
        return json.loads(next(iter(unique)))
    return None


def _decision(path: Path) -> dict[str, Any] | None:
    """The decision object at PATH, or None when nothing readable is there."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return document if isinstance(document, dict) else None


def unanswered_files(shards: list[dict]) -> set[str]:
    """The files no shard either resolved or declined — per FILE, not per shard.

    PROBLEM CLASS — judging a per-file outcome from per-shard records. A file cut
    into blocks has several shards, and a residue retry that finishes the file
    leaves its ORIGINAL block shard still unresolved, so a per-shard reading
    reports a file this run completed. `file_answered` below holds that rule.

    A file with an errored shard is excluded either way — the FAILED line already
    names it, and this is the no-execution-error claim. A file with no shards at
    all is not this question: a NO_DELIVERABLE shard runs over a whole set of
    already-resolved files, and reading those as unanswered would call every one
    of them a fault.

    Both readers of this rule call it: the fan-out, which has the shards in
    memory, and the marker verdict, which reads the same records off disk."""
    unanswered = set()
    for file, file_shards in shards_by_file(shards).items():
        if any(shard.get("is_error") for shard in file_shards):
            continue
        if not file_answered(file_shards):
            unanswered.add(file)
    return unanswered


def shards_by_file(shards: list[dict]) -> dict[str, list[dict]]:
    """SHARDS grouped by the file each one was assigned, in first-seen order."""
    grouped: dict[str, list[dict]] = {}
    for shard in shards:
        grouped.setdefault(shard["file"], []).append(shard)
    return grouped


def file_answered(file_shards: list[dict]) -> bool:
    """Whether one file's shards ANSWERED it — resolved or declined.

    `whole_file` says which shard is which: one that speaks for the WHOLE file
    (an un-cut file, or a residue retry) answers for it outright, because the
    harness already checked the whole file's content for it; short of one, the
    file is answered only when every block answered.

    The one definition both per-file readers call. `unanswered_files` above asks
    it of a file with no execution error, and `_marker_verdict` asks it of a file
    whose shards ran out of clock — a block answering beside a starved sibling
    must not read as an answer for the file in either."""
    whole = next((s for s in file_shards if s.get("whole_file")), None)
    if whole is not None:
        return bool(whole.get("resolved") or whole.get("declined"))
    return all(s.get("resolved") or s.get("declined") for s in file_shards)


def read_verdict(path: Path) -> Any:
    """One shard's modify/delete decision, or None when it did not decide.

    `decline` is a DECISION and rides through here with its reasoning. A
    modify/delete shard records it in this same file, and dropping it would hand
    the caller the one state this tree exists to remove: a shard that judged the
    conflict, reported success, and left the caller unable to tell that from a
    shard that answered nothing."""
    document = _decision(path)
    if document is None or document.get("decision") not in (
        "keep",
        "delete",
        "decline",
    ):
        return None
    return {
        "decision": document["decision"],
        "reasoning": render_number(alt(document.get("reasoning"), "")),
    }


def read_decline(path: Path) -> str | None:
    """The reasoning one shard recorded for leaving its conflict markers, or
    None when it recorded no decline.

    PROBLEM CLASS — telling a model that DECLINED a merge from a shard that
    produced nothing. Both leave the same markers behind and both exit 0, so
    without a record written by the shard itself the harness has to guess, and
    each guess sends a human the wrong way: to finish a merge nobody judged, or
    to file a resolver bug against a judgement.

    An empty reasoning still counts as a decline: the decision is the record,
    and dropping a shard's answer because it wrote a bad sentence would put it
    back in the state this file exists to distinguish."""
    document = _decision(path)
    if document is None or document.get("decision") != "decline":
        return None
    return render_number(alt(document.get("reasoning"), ""))


def render_number(value: Any) -> str:
    """A JSON scalar as jq's `-r` prints it, so a cost or a reasoning field reads
    the same from either implementation."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return json.dumps(value)
