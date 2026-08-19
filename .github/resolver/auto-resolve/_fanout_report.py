"""Everything a conflict fan-out says out loud, and the words it says it in.

PROBLEM CLASS — a paid run whose only account of itself is a JSON file nobody
opens. A shard that errored, a shard that ran and answered nothing, and the
spend the run made are each invisible in the step log unless something prints
them, so a maintainer reads a green run and a pull request with its conflict
markers still in place. The wording of every one of those lines lives here; the
fan-out that produces the records stays in fanout.py.

Two rules cross every line below. Untrusted text is DEFANGED, because a line
beginning `::` is a workflow command the runner executes. And a PROVISIONAL
attempt — one whose terminal verdict a credential ladder owns — drops the
`::error::` prefix, so a rung that loses leaves no red annotation on a run a
later rung wins.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _result_fields import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    render_number,
    unanswered_files,
)


def die(message: str, code: int = 1) -> NoReturn:
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(code)


def defanged(text: str) -> str:
    """TEXT capped and made safe to write to the step log. A line beginning
    `::` is a workflow command the runner EXECUTES; one leading space stops
    untrusted content from raising its own `::error::`."""
    return re.sub(r"^::", " ::", text[:8192], flags=re.MULTILINE)


def silent_shards(shards: list[dict]) -> list[dict]:
    """The shards that RAN, reported success and answered nothing at all.

    A shard has exactly three outcomes: it delivers a marker-free
    resolution, it records a decline, or it is this. The first two are
    answers a human or a later step can act on; this one is the harness
    falling over while reporting success, so the run must not pass it off as
    either. `is_error` shards are excluded because their own FAILED line
    already names them and the credential ladder owns them.

    Scoped to the files NOTHING answered, so a residue retry that finished
    the file makes its earlier silence cost nothing — the same reading a
    non-zero exit gets when the deliverable survived it.
    """
    # The harness-fault set: the run billed for these files and got back
    # neither a resolution nor a reason.
    unanswered = unanswered_files(shards)
    return [
        shard
        for shard in shards
        if shard["file"] in unanswered
        and not (shard["resolved"] or shard["declined"] or shard["is_error"])
    ]


def silence_cause(run, shard: dict) -> str:
    """Which way shard SHARD of fan-out RUN produced nothing, named by the path
    that should have held its answer. A refusal that only says "no deliverable"
    sends its reader to the run log to work out which of these it was."""
    index = shard["index"]
    work = run.work[index]
    if work.path in run.modify_delete:
        return f"it wrote no keep/delete/decline verdict to {run.verdict_path(index)}"
    target = Path(run.resolved_path(index) if run.delivers_out(work) else work.path)
    where = "its scratch path" if run.delivers_out(work) else "the file itself"
    if not target.is_file() or not target.stat().st_size:
        return (
            f"it wrote nothing to {where} ({target}) and recorded no decline "
            f"at {run.decline_path(index)}"
        )
    return (
        f"{where} ({target}) still carries conflict markers and it recorded "
        f"no decline at {run.decline_path(index)}"
    )


def report(run) -> None:
    """Surface each failed shard of fan-out RUN by name, so an errored
    sub-resolution is visible in the step log and not only inside the aggregate
    JSON. RUN is fanout.py's own, taken as an argument rather than imported, so
    the wording lives here and the fan-out stays there."""
    try:
        document = json.loads(run.aggregate_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        die(f"could not read the aggregate execution log {run.aggregate_file}.")
    shards = document["shards"]
    # A credential ladder judges the attempt after this process exits. GitHub
    # keeps workflow-command annotations from every continue-on-error rung, so
    # an early dead token otherwise leaves red "failure" annotations on a
    # successful run. Keep the evidence in the log; the ladder's final gate
    # owns the annotation when no credential succeeds.
    provisional = os.environ.get("PROVISIONAL_ATTEMPT") == "true"
    failure_prefix = (
        "conflict resolution FAILED"
        if provisional
        else "::error::conflict resolution FAILED"
    )
    unanswered = unanswered_files(shards)
    for shard in silent_shards(shards):
        # Same `provisional` rule as failure_prefix above: a rung that loses
        # must not leave red on a run a later rung wins, and this line is
        # reachable on a non-final rung — one file erroring advances the
        # ladder while another file's shard answers nothing.
        print(
            f"{'' if provisional else '::error::'}{shard['file']} shard "
            f"{shard['index']} ran and reported success but answered "
            f"NOTHING: {silence_cause(run, shard)}",
            file=sys.stderr,
        )
    for shard in shards:
        if not shard["is_error"]:
            if shard["declined"]:
                print(
                    f"::notice::{shard['file']} shard {shard['index']} declined "
                    f"its assignment: {defanged(shard['decline_reason'])}",
                    file=sys.stderr,
                )
            # A shard whose process failed and whose resolution survived it.
            # Said out loud because the run then reports success with a
            # non-zero exit in its log, and a reader who cannot see why reads
            # that as the gate having missed a failure.
            if shard["exit_status"] != 0 and shard["resolved"]:
                print(
                    f"::notice::{shard['file']} was resolved despite shard exit "
                    f"{shard['exit_status']} — the deliverable the shard wrote "
                    "is complete, so its process dying afterwards cost nothing",
                    file=sys.stderr,
                )
            continue
        print(
            f"{failure_prefix} for {shard['file']} (shard exit {shard['exit_status']})",
            file=sys.stderr,
        )
        # The shard's OWN account of why it failed; otherwise it reaches
        # the maintainer as `(shard exit 1)` and nothing else.
        if shard["api_error_status"] is not None:
            print(f"  API status: {shard['api_error_status']}", file=sys.stderr)
        if shard["error_text"]:
            sys.stderr.write(f"  {defanged(shard['error_text'])}\n")
        # FANOUT_DIR is gone once the job ends; stderr must reach here too.
        errors = run.dir / f"{shard['index']}.stderr"
        if errors.is_file() and errors.stat().st_size > 0:
            body = errors.read_bytes()[:8192].decode("utf-8", "replace")
            sys.stderr.write(defanged(body))
    # Counted off `resolved`, not `is_error`: a shard that exits 0 having
    # written nothing bills for the run and leaves the file conflicted, so
    # it is not `ok` here.
    ok = sum(1 for shard in shards if shard["resolved"])
    errored = sum(1 for shard in shards if shard["is_error"])
    declined = sum(1 for shard in shards if shard["declined"])
    # Per FILE, not per shard, for the same reason as the errors above: a
    # residue retry that finished a file must not keep it in this count
    # because its original block shard still reads unresolved.
    unanswered_count = len(unanswered)
    # A shard that could not report its spend takes the aggregate's key away,
    # so the total the reader gets is a LOWER BOUND over the shards that
    # could. `+?` is what says so: printing the bound bare would read as the
    # whole bill, and printing nothing hides spend the run did make.
    known = [s["total_cost_usd"] for s in shards if s["total_cost_usd"] is not None]
    cost = (
        f"${render_number(document['total_cost_usd'])}"
        if "total_cost_usd" in document
        else f"${render_number(sum(known))}+?"
    )
    denials = document["permission_denials_count"]
    line = (
        f"ran {len(shards)} shard(s) across {len(run.files)} file(s): "
        f"{ok} resolved, {errored} errored, {declined} declined, "
        f"{unanswered_count} unanswered; "
        f"cost {cost}, {denials} permission denial(s)"
    )
    if denials > 0:
        names = document["permission_denied_tools"]
        line += f" on {'unnamed tool(s)' if names is None else ', '.join(names)}"
    print(line, file=sys.stderr)
