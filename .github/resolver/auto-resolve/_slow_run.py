"""Did this run spend more wall clock than its conflict justified?

PROBLEM CLASS — a resolve that SUCCEEDS slowly reports nothing. The job budget
(`auto-resolve.yaml`'s `timeout-minutes`) only fires when a run blows through it, and
a run killed there pushes nothing, so the one signal the repository gets is silence
followed by a lost resolution. Between "fast" and "killed" sits the case nobody sees:
a one-file conflict that took forty minutes because a toolchain install hung. That is
a defect in this workflow, not a hard conflict, and it looks identical to success.

The timings are GITHUB'S, not the resolver's own. `land` is a separate job, so by the
time it runs the resolve job has finished and `GET /actions/runs/{id}/jobs` carries
every step's `started_at`. A resolver that timed itself could not report the stage
that killed it, and would need a stamping step in a job whose whole trust model is
that it runs no privileged code. A job GitHub kills for exceeding its own
`timeout-minutes` can still leave a step's `completed_at` empty in that same API
response — exactly the hung stage the advisory exists to catch — so such a step is
timed against its JOB's own end and reported as unfinished rather than dropped.

Two verdicts, and both thresholds are values `auto-resolve.yaml` already sets, so this
module adds no number of its own:

  * A STAGE over the advisory ceiling. 1200 seconds is the largest share any stage is
    budgeted (`FANOUT_BUDGET_SECONDS`, `SELF_REVIEW_BUDGET_SECONDS`), and both of those
    stages are bounded by it, so a step past it is one nothing bounded. Every other
    stage's documented share is smaller still. That makes the ceiling precise: it
    cannot fire for a stage spending what it was promised.
  * The RUN too long for the conflict's SIZE. Shards run `MAX_PARALLEL` at a time and
    each is killed at `SHARD_TIMEOUT_SECONDS`, so a conflict over N files is at most
    `ceil(N / MAX_PARALLEL)` waves. Anything past that plus the non-model stages'
    documented share is time the conflict did not ask for.

Pure functions over already-read values: the caller owns every read. Standard library
only, like its siblings — the resolve job runs the system python3.
"""

import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: The largest share `auto-resolve.yaml` gives any single stage. A stage past this
#: spent time nothing granted it, which is what makes the alert high-precision.
STAGE_CEILING_SECONDS = 1200

#: What the budget reserves for everything that is not a resolution wave — discover,
#: both checkouts, the toolchain installs, and staging the fan-out logs.
NON_WAVE_SECONDS = 7 * 60


@dataclass(frozen=True)
class TimedStep:
    """One step of the resolve job, as the jobs API reports it.

    `unfinished` is true for a step the API gave no `completed_at` — the stage most
    likely to be the slow one, and the one GitHub killed when its job hit the budget.
    """

    name: str
    seconds: int
    unfinished: bool = False


def whole_or(raw: str | None, fallback: int) -> int:
    """A positive whole number from the environment, or the caller's fallback.

    An unset or malformed bound must not make the verdict stricter than the run it
    judges: a zero would report every run as over budget, so the fallback is the
    value `auto-resolve.yaml` sets for it.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def write_sidecar(bundle_dir: Path, files: int) -> None:
    """Record what the verdict needs and `land` cannot re-derive.

    The conflicted set and the two bounds are the RESOLVE job's, so they are read
    here rather than restated in the landing job, which has neither. Creates
    `bundle_dir`: the caller writes this before any stage that could hang, so a
    later kill still leaves the sizes on disk for `land` to read.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "slow-run.json").write_text(
        json.dumps(
            {
                "files": files,
                "max_parallel": whole_or(os.environ.get("MAX_PARALLEL"), 1),
                "shard_timeout": whole_or(os.environ.get("SHARD_TIMEOUT_SECONDS"), 600),
                "fanout_budget": whole_or(
                    os.environ.get("FANOUT_BUDGET_SECONDS"), STAGE_CEILING_SECONDS
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _at(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def steps_of(
    jobs: list[dict], job_name: str, now: datetime | None = None
) -> list[TimedStep]:
    """Every step of the named job that has STARTED.

    A step with no `started_at` never began, so there is nothing to time and it is
    skipped. One with no `completed_at` is marked `unfinished` and timed against the
    JOB's own `completed_at` — `land` reads the jobs API only after `resolve` has
    finished, so a step GitHub killed mid-flight ended when its job did, not
    whenever `land` happens to be read. Only a job with no `completed_at` EITHER
    (genuinely still running) falls back to `now`, so a queued or slow `land` run
    can never inflate a step it never asked about.
    """
    now = now or datetime.now(timezone.utc)
    found: list[TimedStep] = []
    for job in jobs:
        if job.get("name") != job_name:
            continue
        job_end = _at(job.get("completed_at")) or now
        for step in job.get("steps") or []:
            began, ended = _at(step.get("started_at")), _at(step.get("completed_at"))
            if began is None:
                continue
            unfinished = ended is None
            ended = ended or job_end
            seconds = max(int((ended - began).total_seconds()), 0)
            found.append(TimedStep(str(step.get("name", "")), seconds, unfinished))
    return found


def waves_for(files: int, max_parallel: int) -> int:
    """Resolution waves a conflict over `files` paths takes at `max_parallel` shards."""
    return math.ceil(max(files, 1) / max(max_parallel, 1))


def expected_seconds(
    files: int, max_parallel: int, shard_timeout: int, fanout_budget: int
) -> int:
    """The most wall clock a conflict over `files` paths can honestly need.

    CAPPED at the fan-out's own budget, which bounds the fan-out as a WHOLE rather
    than per wave: a wide conflict runs its waves inside that one budget, so counting
    a full `shard_timeout` per wave would grant a 12-file conflict time the workflow
    never gives it, and the alert would then never fire for the widest runs.
    """
    waves = waves_for(files, max_parallel) * max(shard_timeout, 0)
    return min(waves, max(fanout_budget, 0)) + NON_WAVE_SECONDS


def _minutes(seconds: int) -> str:
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _spent(step: TimedStep) -> str:
    """A step's reported time, marked as unfinished when it carries no end stamp."""
    spent = _minutes(step.seconds)
    return f"{spent}, unfinished" if step.unfinished else spent


def finding(
    steps: list[TimedStep],
    files: int,
    max_parallel: int,
    shard_timeout: int,
    fanout_budget: int = STAGE_CEILING_SECONDS,
    *,
    landed: bool = True,
) -> str:
    """The alert `land` publishes, or an empty string when the run was in budget.

    Never a refusal. A slow resolution that is CORRECT must still land: refusing would
    throw away the paid resolution and hand a human back the conflict AND the defect,
    which is the trade `_post_merge_check.py` already settled the same way. `landed`
    is false for a run that produced no bundle at all — killed by GitHub, or still
    running when this is read — so the lead makes no claim about a push.
    """
    if not steps or files <= 0:
        return ""
    over = [step for step in steps if step.seconds > STAGE_CEILING_SECONDS]
    total = sum(step.seconds for step in steps)
    budget = expected_seconds(files, max_parallel, shard_timeout, fanout_budget)
    if not over and total <= budget:
        return ""
    if landed:
        lines = [
            "⚠️ **Auto-resolve resolved this conflict, and took longer than the "
            "conflict explains** — the resolution is correct and pushed; this is a "
            "defect report about the run that produced it."
        ]
    else:
        lines = [
            "⚠️ **Auto-resolve has not pushed a resolution for this conflict, and "
            "has already spent longer than the conflict explains** — read the "
            "resolve job for why; this reports the time it spent."
        ]
    if over:
        named = ", ".join(f"`{step.name}` ({_spent(step)})" for step in over)
        lines.append(
            f"Past the {_minutes(STAGE_CEILING_SECONDS)} advisory ceiling, which is the "
            f"largest share any stage is budgeted: {named}. Nothing bounds a stage that "
            "reaches this, so a conflict of the same shape can exhaust the whole job "
            "budget and push nothing."
        )
    if total > budget:
        lines.append(
            f"The run spent {_minutes(total)} on a {files}-file conflict, against "
            f"{_minutes(budget)} that size can justify — "
            f"{waves_for(files, max_parallel)} resolution wave(s) at "
            f"{_minutes(shard_timeout)} each, capped at the fan-out's own "
            f"{_minutes(fanout_budget)}, plus {_minutes(NON_WAVE_SECONDS)} for the "
            "checkouts, the toolchain installs and staging the logs."
        )
    return "\n\n".join(lines) + "\n"


def main() -> None:
    """`_slow_run.py <jobs.json> <slow-run.json> <resolve-job-name> [landed]` → the
    finding. LANDED defaults to true; the caller passes "false" for a run that
    reached no bundle at all.

    Prints nothing when the run was in budget, so the caller can test for empty
    output. Every read that could fail — an unreadable file, a shape the API changed
    — yields NO finding rather than a wrong one: this is an advisory beside a
    resolution that already landed, so a false alarm costs more than a missed one.
    """
    try:
        jobs_doc = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        sizes = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    except (OSError, ValueError, IndexError):
        return
    # `gh api --paginate --slurp` yields a LIST OF PAGES, each `{"jobs": [...]}`.
    pages = jobs_doc if isinstance(jobs_doc, list) else [jobs_doc]
    jobs = [job for page in pages for job in (page or {}).get("jobs") or []]
    print(
        finding(
            steps_of(jobs, sys.argv[3]),
            whole_or(sizes.get("files"), 0),
            whole_or(sizes.get("max_parallel"), 1),
            whole_or(sizes.get("shard_timeout"), 600),
            whole_or(sizes.get("fanout_budget"), STAGE_CEILING_SECONDS),
            landed=len(sys.argv) < 5 or sys.argv[4] != "false",
        ),
        end="",
    )


if __name__ == "__main__":
    main()
