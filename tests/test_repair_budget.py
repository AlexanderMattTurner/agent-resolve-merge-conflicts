"""The hook-repair pass's wall clock.

PROBLEM CLASS — a stage that takes a fixed slice of a shared budget starves when
an earlier stage under-spends. The resolve job's timeout is derived as the SUM of
its stages, so a fan-out that finishes early leaves time no later stage can
reach. agent-glovebox PR #5009 resolved every conflict with minutes of its
fan-out window unspent, then died on this pass at exactly its 600-second cap.
"""

# covers: .github/resolver/auto-resolve/_hook_gate.py

import pytest

from tests._resolver_helpers import load_script

hook_gate = load_script(".github/resolver/auto-resolve/_hook_gate.py")

DEADLINE = "AUTO_RESOLVE_FANOUT_DEADLINE_EPOCH"


def test_the_pass_keeps_its_own_bound_when_no_fanout_ran(monkeypatch) -> None:
    """A deterministic-only resolve publishes no deadline, so there is nothing to
    donate and the configured bound stands alone."""
    monkeypatch.delenv(DEADLINE, raising=False)
    monkeypatch.setenv("SHARD_TIMEOUT_SECONDS", "600")

    assert hook_gate.repair_budget_seconds(now=1000.0) == 600


def test_the_pass_claims_what_the_fanout_left_unspent(monkeypatch) -> None:
    """The fan-out stamped a deadline 300s out and finished early, so the repair
    gets its own 600 plus those 300 — and the job's total is unchanged, because
    the 300 were already promised to the fan-out."""
    monkeypatch.setenv(DEADLINE, "1300")
    monkeypatch.setenv("SHARD_TIMEOUT_SECONDS", "600")

    assert hook_gate.repair_budget_seconds(now=1000.0) == 900


def test_a_fanout_that_spent_its_whole_window_donates_nothing(monkeypatch) -> None:
    """A deadline already past must not SUBTRACT from the pass's own bound: the
    fan-out overrunning is not a reason to shorten the repair below its floor."""
    monkeypatch.setenv(DEADLINE, "900")
    monkeypatch.setenv("SHARD_TIMEOUT_SECONDS", "600")

    assert hook_gate.repair_budget_seconds(now=1000.0) == 600


@pytest.mark.parametrize(
    "value",
    ["", "   ", "later", "-300", "12.5", "٣٠٠"],
    ids=["empty", "blank", "word", "negative", "fractional", "arabic-indic digits"],
)
def test_an_unreadable_deadline_donates_nothing(monkeypatch, value: str) -> None:
    """The donation is an optimisation, so a stamp this cannot read leaves the
    configured bound exactly as it was — never a crash, and never a larger
    budget than the job was sized for. `str.isdigit()` is true for Unicode
    digits that `int()` then accepts, which is why the check is ASCII-only."""
    monkeypatch.setenv(DEADLINE, value)
    monkeypatch.setenv("SHARD_TIMEOUT_SECONDS", "600")

    assert hook_gate.repair_budget_seconds(now=1000.0) == 600
