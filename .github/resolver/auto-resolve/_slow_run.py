"""Did this run spend more wall clock than its conflict justified?

PROBLEM CLASS — a resolve that SUCCEEDS slowly reports nothing. The job budget
(`auto-resolve.yaml`'s `timeout-minutes`) only fires when a run blows through it, and
a run killed there pushes nothing, so the one signal the repository gets is silence
followed by a lost resolution. Between "fast" and "killed" sits the case nobody sees:
a one-file conflict that took forty minutes because a toolchain install hung. That is
a defect in this workflow, not a hard conflict, and it looks identical to success.

The timings are GITHUB'S, not the resolver's own. `land` is a separate job, so by the
time it runs the resolve job has finished and `GET /actions/runs/{id}/jobs` carries
every step's `started_at` and `completed_at`. A resolver that timed itself could not
report the stage that killed it, and would need a stamping step in a job whose whole
trust model is that it runs no privileged code.

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
from datetime import datetime
from pathlib import Path

#: The largest share `auto-resolve.yaml` gives any single stage. A stage past this
#: spent time nothing granted it, which is what makes the alert high-precision.
STAGE_CEILING_SECONDS = 1200

#: What the budget reserves for everything that is not a resolution wave — discover,
#: both checkouts, the toolchain installs, and staging the fan-out logs.
NON_WAVE_SECONDS = 7 * 60


@dataclass(frozen=True)
class TimedStep:
    """One step of the resolve job, as the jobs API reports it."""

    name: str
    seconds: int


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
    here rather than restated in the landing job, which has neither.
    """
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


def steps_of(jobs: list[dict], job_name: str) -> list[TimedStep]:
    """Every step of the named job that reported both of its timestamps.

    A step missing either one is DROPPED rather than counted as zero: a step still
    running has no `completed_at`, and reading that as instant would hide the very
    stage most likely to be the slow one.
    """
    found: list[TimedStep] = []
    for job in jobs:
        if job.get("name") != job_name:
            continue
        for step in job.get("steps") or []:
            began, ended = _at(step.get("started_at")), _at(step.get("completed_at"))
            if began is None or ended is None:
                continue
            found.append(
                TimedStep(str(step.get("name", "")), int((ended - began).total_seconds()))
            )
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


def finding(
    steps: list[TimedStep],
    files: int,
    max_parallel: int,
    shard_timeout: int,
    fanout_budget: int = STAGE_CEILING_SECONDS,
) -> str:
    """The alert `land` publishes, or an empty string when the run was in budget.

    Never a refusal. A slow resolution that is CORRECT must still land: refusing would
    throw away the paid resolution and hand a human back the conflict AND the defect,
    which is the trade `_post_merge_check.py` already settled the same way.
    """
    if not steps or files <= 0:
        return ""
    over = [step for step in steps if step.seconds > STAGE_CEILING_SECONDS]
    total = sum(step.seconds for step in steps)
    budget = expected_seconds(files, max_parallel, shard_timeout, fanout_budget)
    if not over and total <= budget:
        return ""
    lines = [
        "⚠️ **Auto-resolve resolved this conflict, and took longer than the conflict "
        "explains** — the resolution is correct and pushed; this is a defect report "
        "about the run that produced it."
    ]
    if over:
        named = ", ".join(f"`{step.name}` ({_minutes(step.seconds)})" for step in over)
        lines.append(
            f"Past the {_minutes(STAGE_CEILING_SECONDS)} advisory ceiling, which is the "
            f"largest share any stage is budgeted: {named}. Nothing bounds a stage that "
            "reaches this, so the next conflict of the same shape can exhaust the whole "
            "job budget and push nothing."
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
    """`_slow_run.py <jobs.json> <slow-run.json> <resolve-job-name>` → the finding.

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
        ),
        end="",
    )


if __name__ == "__main__":
    main()
