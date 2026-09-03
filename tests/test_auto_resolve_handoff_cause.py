"""Tests for the HANDOFF CAUSE a mark carries (agent-glovebox #5644: one head
that drew five identical handoffs because nothing recorded what the run ran out
of).

The pure half is driven with status lists shaped like the GitHub API's, and the
live half with a real `mark-handoff.sh` and a recording `gh` on PATH."""

import json
import os
import stat
import subprocess
from pathlib import Path

from tests._resolver_helpers import (
    REPO_ROOT,
    current_path,
    load_script,
    record_gh_call,
)

handoff_cause = load_script(".github/resolver/auto-resolve/_handoff_cause.py")
HANDOFF = handoff_cause.HANDOFF_CONTEXT
SHARD_TIMEOUT = handoff_cause.SHARD_TIMEOUT
FANOUT_BUDGET = handoff_cause.FANOUT_BUDGET

# GitHub rejects a commit status whose description is longer than this, and it
# rejects the whole write — so an overlong cause would cost the mark itself.
_DESCRIPTION_MAX = 140


def _mark(context: str, cause: str = "") -> dict:
    """One status as the API returns it, carrying CAUSE the way a run records it."""
    return {
        "context": context,
        "description": f"auto-resolve left the rest to a human{handoff_cause.suffix(cause)}",
    }


def test_a_recorded_cause_is_read_back_off_the_mark_description():
    # The whole mechanism in one line: the run that refuses writes the cause, and
    # the next run on the same head reads it back.
    statuses = [_mark(HANDOFF, SHARD_TIMEOUT), _mark(HANDOFF, FANOUT_BUDGET)]
    assert handoff_cause.causes_in(statuses, HANDOFF) == [
        SHARD_TIMEOUT,
        FANOUT_BUDGET,
    ]


def test_a_mark_on_another_context_is_not_counted():
    # The attempt mark rides the same statuses read and is written on every run,
    # so counting it would escalate a head that has refused nothing.
    statuses = [_mark("auto-resolve/attempted", SHARD_TIMEOUT), _mark(HANDOFF)]
    assert handoff_cause.causes_in(statuses, HANDOFF) == []


def test_the_first_repeat_gets_a_longer_per_shard_window():
    assert handoff_cause.escalation_for(()) == 1
    assert handoff_cause.escalation_for((SHARD_TIMEOUT,)) > 1


def test_a_cause_no_longer_window_addresses_never_escalates():
    # The fan-out's own wall clock. A longer per-shard window makes that worse, so
    # this cause is recorded and left to the resolver change discover already
    # retires a handoff on.
    assert handoff_cause.escalation_for((FANOUT_BUDGET,)) == 1
    assert not handoff_cause.escalation_is_spent((FANOUT_BUDGET,), FANOUT_BUDGET)


def test_the_refusal_after_the_escalated_run_declines():
    # One prior handoff means the run in between ran escalated and answered the
    # same way, so this refusal stops the spend instead of repeating it.
    assert not handoff_cause.escalation_is_spent((), SHARD_TIMEOUT)
    assert handoff_cause.escalation_is_spent((SHARD_TIMEOUT,), SHARD_TIMEOUT)


def test_the_escalated_run_that_stops_on_a_different_clock_still_declines():
    # The escalated run gives EVERY shard the longer window, so it can run out of
    # the fan-out's whole budget where the run before it ran out of one shard's.
    # Counting repeats of one cause would read that as two first refusals and
    # escalate this head for as long as the two alternated.
    assert handoff_cause.escalation_is_spent((SHARD_TIMEOUT,), FANOUT_BUDGET)


def _gh_shim(tmp_path, body: str) -> tuple[str, str]:
    """A recording `gh` on a PATH, returning that PATH and the call log."""
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    log = tmp_path / "gh-calls"
    log.write_text("", encoding="utf-8")
    gh = shim_dir / "gh"
    gh.write_text(
        f"#!/usr/bin/env bash\n{record_gh_call(str(log))}{body}\n", encoding="utf-8"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return f"{shim_dir}:{current_path()}", str(log)


def test_a_status_read_that_fails_reads_as_no_prior_cause(tmp_path, monkeypatch):
    # Fail OPEN. The escalation is an optimisation; a head whose statuses cannot
    # be read must still draw the plain handoff it drew before this existed.
    path, _ = _gh_shim(tmp_path, "exit 1")
    monkeypatch.setenv("PATH", path)
    monkeypatch.setenv("GH_REPO", "owner/repo")
    monkeypatch.setenv("HEAD_SHA", "deadbeef")
    handoff_cause.head_handoff_causes.cache_clear()
    assert handoff_cause.head_handoff_causes() == ()
    assert handoff_cause.escalated_shard_timeout(600) == 600
    assert not handoff_cause.mark_should_decline(SHARD_TIMEOUT)
    handoff_cause.head_handoff_causes.cache_clear()


def test_a_caller_that_names_no_head_warns_about_no_failed_read(
    tmp_path, monkeypatch, capsys
):
    """A caller with no repository and no head asked nothing, so nothing failed.

    The warning names a read that could not be made against a real head. Printing
    it here would put a line on the stdout of every caller that resolves without
    one, which the golden-record equivalence suite compares byte for byte.
    """
    path, _ = _gh_shim(tmp_path, "exit 1")
    monkeypatch.setenv("PATH", path)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.delenv("HEAD_SHA", raising=False)
    handoff_cause.head_handoff_causes.cache_clear()

    assert handoff_cause.head_handoff_causes() == ()
    assert capsys.readouterr().out == ""
    handoff_cause.head_handoff_causes.cache_clear()


def test_a_head_that_already_handed_off_escalates_and_then_declines(
    tmp_path, monkeypatch
):
    # The live path, end to end: a real `gh` read of this head's own statuses
    # decides both the longer window and the decline.
    answer = json.dumps([_mark(HANDOFF, SHARD_TIMEOUT)]).replace("'", "'\\''")
    path, _ = _gh_shim(tmp_path, f"printf '%s' '{answer}'")
    monkeypatch.setenv("PATH", path)
    monkeypatch.setenv("GH_REPO", "owner/repo")
    monkeypatch.setenv("HEAD_SHA", "deadbeef")
    handoff_cause.head_handoff_causes.cache_clear()
    assert handoff_cause.escalated_shard_timeout(600) > 600
    assert handoff_cause.mark_should_decline(SHARD_TIMEOUT)
    handoff_cause.head_handoff_causes.cache_clear()


def test_the_mark_the_shell_writes_carries_the_cause_and_fits_githubs_cap(tmp_path):
    # The two halves meet here: Python composes the suffix and the shell appends
    # it to a description GitHub must accept whole.
    path, log = _gh_shim(tmp_path, "exit 0")
    done = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / ".github/resolver/auto-resolve/mark-handoff.sh"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": path,
            "REPO": "owner/repo",
            "HEAD_SHA": "deadbeef",
            "GH_TOKEN": "x",
            "AUTO_RESOLVE_HANDOFF_CAUSE_SUFFIX": handoff_cause.suffix(SHARD_TIMEOUT),
        },
    )
    assert done.returncode == 0, done.stderr
    calls = Path(log).read_text(encoding="utf-8").splitlines()
    posted = [line for line in calls if "statuses/deadbeef" in line]
    assert posted, done.stdout + done.stderr
    # The shim records the argv space-separated, so the description runs from its
    # own `-f` value to the next flag `commit_status_mark_set` passes.
    described = posted[0].split("description=", 1)[1].split(" target_url=", 1)[0]
    assert f"[cause={SHARD_TIMEOUT}]" in described, described
    assert len(described) <= _DESCRIPTION_MAX, len(described)
