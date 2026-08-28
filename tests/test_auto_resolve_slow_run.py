"""The slow-run verdict: when a resolve's wall clock is a defect worth reporting.

Behaviour, never source text — each case drives `finding()` with the timings the jobs
API would report and asserts what the author is told.

# covers: .github/resolver/auto-resolve/_slow_run.py
"""

import json

from tests._resolver_helpers import load_script

slow_run = load_script(".github/resolver/auto-resolve/_slow_run.py")

# One wave of a one-file conflict, plus the non-wave stages, is what fits the budget.
ONE_FILE_BUDGET = 600 + slow_run.NON_WAVE_SECONDS


def _steps(*pairs):
    return [slow_run.TimedStep(name, seconds) for name, seconds in pairs]


def test_a_run_inside_its_budget_says_nothing():
    steps = _steps(("Resolve", 300), ("Bundle", 60))
    assert slow_run.finding(steps, files=1, max_parallel=4, shard_timeout=600) == ""


def test_a_one_file_conflict_that_ran_long_is_reported():
    steps = _steps(("Install the pinned hook toolchain", ONE_FILE_BUDGET + 60))
    said = slow_run.finding(steps, files=1, max_parallel=4, shard_timeout=600)
    assert "took longer than the conflict explains" in said
    assert "1-file conflict" in said


def test_a_wide_conflict_gets_more_room_than_a_narrow_one():
    # 12 files at 4 in flight is three waves, so this size buys the fan-out's whole
    # budget where one file buys a single shard's.
    wide = slow_run.expected_seconds(12, 4, 600, 1200)
    narrow = slow_run.expected_seconds(1, 4, 600, 1200)
    assert wide > narrow
    steps = _steps(("Resolve the conflict", 1200), ("Bundle", 300))
    assert slow_run.finding(steps, 12, 4, 600, 1200) == ""


def test_the_budget_never_grants_more_than_the_fan_out_is_given():
    # Counting a full shard timeout per wave would hand a 60-file conflict ten times
    # the fan-out's own budget, and the alert would then never fire for a wide run.
    assert (
        slow_run.expected_seconds(60, 4, 600, 1200) == 1200 + slow_run.NON_WAVE_SECONDS
    )


def test_a_stage_past_the_advisory_ceiling_is_named():
    steps = _steps(("Set up uv", slow_run.STAGE_CEILING_SECONDS + 1))
    said = slow_run.finding(steps, files=12, max_parallel=4, shard_timeout=600)
    assert "`Set up uv`" in said
    assert "advisory ceiling" in said


def test_a_stage_spending_exactly_its_largest_share_is_not_a_finding():
    # The precision claim: the two stages budgeted 1200s are bounded AT 1200s, so
    # the ceiling must not fire for one that spent what it was promised.
    steps = _steps(("Resolve the conflict", slow_run.STAGE_CEILING_SECONDS))
    assert slow_run.finding(steps, files=12, max_parallel=4, shard_timeout=600) == ""


def test_an_unrecorded_conflict_size_reports_nothing():
    steps = _steps(("Resolve", 10_000))
    assert slow_run.finding(steps, files=0, max_parallel=4, shard_timeout=600) == ""


def test_a_step_still_running_is_dropped_rather_than_counted_as_instant():
    jobs = [
        {
            "name": "Auto-resolve merge conflicts",
            "steps": [
                {
                    "name": "done",
                    "started_at": "2026-08-28T18:00:00Z",
                    "completed_at": "2026-08-28T18:05:00Z",
                },
                {"name": "still going", "started_at": "2026-08-28T18:05:00Z"},
            ],
        }
    ]
    steps = slow_run.steps_of(jobs, "Auto-resolve merge conflicts")
    assert [(step.name, step.seconds) for step in steps] == [("done", 300)]


def test_only_the_named_job_is_read():
    jobs = [
        {
            "name": "Land the auto-resolved merge",
            "steps": [
                {
                    "name": "landing",
                    "started_at": "2026-08-28T18:00:00Z",
                    "completed_at": "2026-08-28T18:40:00Z",
                }
            ],
        }
    ]
    assert slow_run.steps_of(jobs, "Auto-resolve merge conflicts") == []


def test_a_missing_bound_falls_back_instead_of_reporting_every_run():
    # A zero would make the budget zero, which reports every run as over.
    assert slow_run.whole_or("", 600) == 600
    assert slow_run.whole_or("0", 600) == 600
    assert slow_run.whole_or(None, 4) == 4
    assert slow_run.whole_or("8", 4) == 8


def test_the_cli_reads_a_paginated_slurp_and_prints_the_finding(tmp_path, capsys):
    # `gh api --paginate --slurp` yields a LIST OF PAGES, which is the shape the
    # caller actually hands over.
    jobs = tmp_path / "jobs.json"
    jobs.write_text(
        json.dumps(
            [
                {
                    "jobs": [
                        {
                            "name": "Auto-resolve merge conflicts",
                            "steps": [
                                {
                                    "name": "Set up uv",
                                    "started_at": "2026-08-28T18:00:00Z",
                                    "completed_at": "2026-08-28T18:40:00Z",
                                }
                            ],
                        }
                    ]
                }
            ]
        ),
        encoding="utf-8",
    )
    sizes = tmp_path / "slow-run.json"
    sizes.write_text(
        json.dumps({"files": 1, "max_parallel": 4, "shard_timeout": 600}),
        encoding="utf-8",
    )
    slow_run.sys.argv = [
        "_slow_run.py",
        str(jobs),
        str(sizes),
        "Auto-resolve merge conflicts",
    ]
    slow_run.main()
    assert "advisory ceiling" in capsys.readouterr().out


def test_the_sidecar_records_the_resolve_job_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_PARALLEL", "6")
    monkeypatch.setenv("SHARD_TIMEOUT_SECONDS", "900")
    monkeypatch.delenv("FANOUT_BUDGET_SECONDS", raising=False)
    slow_run.write_sidecar(tmp_path, 3)
    assert json.loads((tmp_path / "slow-run.json").read_text(encoding="utf-8")) == {
        "files": 3,
        "max_parallel": 6,
        "shard_timeout": 900,
        "fanout_budget": slow_run.STAGE_CEILING_SECONDS,
    }


def test_an_unreadable_input_prints_no_finding(tmp_path, capsys):
    slow_run.sys.argv = ["_slow_run.py", str(tmp_path / "absent.json"), "x", "y"]
    slow_run.main()
    assert capsys.readouterr().out == ""
