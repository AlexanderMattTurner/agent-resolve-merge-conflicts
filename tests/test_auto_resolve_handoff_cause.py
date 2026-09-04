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
    # so counting it would decline a head that has refused nothing.
    statuses = [_mark("auto-resolve/attempted", SHARD_TIMEOUT), _mark(HANDOFF)]
    assert handoff_cause.causes_in(statuses, HANDOFF) == []


def test_the_first_refusal_hands_off_and_the_repeat_of_it_declines():
    # The whole rule: nothing the resolver reads changed between the two runs, so
    # the second stops where the first stopped and there is no third.
    assert not handoff_cause.cause_is_settled((), SHARD_TIMEOUT)
    assert handoff_cause.cause_is_settled((SHARD_TIMEOUT,), SHARD_TIMEOUT)


def test_each_cause_is_settled_on_its_own_second_sighting():
    # Two causes are two different things the run ran out of, so a head that has
    # refused on one clock has learned nothing yet about the other.
    assert not handoff_cause.cause_is_settled((SHARD_TIMEOUT,), FANOUT_BUDGET)
    assert handoff_cause.cause_is_settled((SHARD_TIMEOUT, FANOUT_BUDGET), FANOUT_BUDGET)


def test_a_cause_this_module_does_not_know_never_declines():
    # The mark records only the causes named here, so a description carrying
    # anything else is one this module cannot have written. Declining on it would
    # strand the head on a record nothing in this repo produced.
    assert not handoff_cause.cause_is_settled(("weather",), "weather")


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
    # Fail OPEN. A head whose statuses cannot be read must still draw the plain
    # handoff it drew before this existed, rather than a decline nobody can
    # retire without pushing to the branch.
    path, _ = _gh_shim(tmp_path, "exit 1")
    monkeypatch.setenv("PATH", path)
    monkeypatch.setenv("GH_REPO", "owner/repo")
    monkeypatch.setenv("HEAD_SHA", "deadbeef")
    assert handoff_cause.head_handoff_causes() == ()
    assert not handoff_cause.mark_should_decline(SHARD_TIMEOUT)


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

    assert handoff_cause.head_handoff_causes() == ()
    assert capsys.readouterr().out == ""


def test_a_head_that_already_handed_off_for_this_cause_declines(tmp_path, monkeypatch):
    # The live path, end to end: a real `gh` read of this head's own statuses is
    # what turns the second refusal into a decline.
    answer = json.dumps([_mark(HANDOFF, SHARD_TIMEOUT)]).replace("'", "'\\''")
    path, _ = _gh_shim(tmp_path, f"printf '%s' '{answer}'")
    monkeypatch.setenv("PATH", path)
    monkeypatch.setenv("GH_REPO", "owner/repo")
    monkeypatch.setenv("HEAD_SHA", "deadbeef")
    assert handoff_cause.mark_should_decline(SHARD_TIMEOUT)
    assert not handoff_cause.mark_should_decline(FANOUT_BUDGET)


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
