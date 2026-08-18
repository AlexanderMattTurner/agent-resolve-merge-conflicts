""".github/scripts/gen_caller_permissions.py — the calling job's permission ceiling.

Drives the generator's pure `render_caller` over synthetic caller/callee pairs,
asserting the block it renders. The real tree is the round-trip case: the
committed region must already equal what the real `auto-resolve.yaml` implies,
because a caller that grants less than its callee ends the whole run in
`startup_failure` before any job reports.
"""

import importlib.util

import pytest

from tests._helpers import REPO_ROOT

_SRC = REPO_ROOT / ".github" / "scripts" / "gen_caller_permissions.py"
_spec = importlib.util.spec_from_file_location("gen_caller_permissions", _SRC)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

CALL = mod.CALLS[0]
BEGIN, END = mod.markers(CALL)

CALLER = f"""\
jobs:
  resolve:
    permissions:
      {BEGIN}
      contents: read # stale, replaced by the render
      {END}
    uses: ./.github/workflows/auto-resolve.yaml
"""


def callee(**jobs: dict) -> str:
    """A minimal reusable workflow whose jobs declare the given permissions."""
    lines = ["on:", "  workflow_call:", "jobs:"]
    for job_id, block in jobs.items():
        lines.append(f"  {job_id}:")
        lines.append("    permissions:")
        lines += [f"      {scope}: {level}" for scope, level in block.items()]
    return "\n".join(lines) + "\n"


def rendered(callee_text: str) -> list[str]:
    """The caller's ceiling lines after a render, markers stripped."""
    body = mod.render_caller(CALL, CALLER, callee_text)
    inner = body.split(BEGIN + "\n")[1].split(END)[0]
    return [line.strip() for line in inner.splitlines() if line.strip()]


def test_write_beats_read_beats_absent() -> None:
    text = callee(
        resolve={"contents": "read", "issues": "write", "statuses": "none"},
        land={"contents": "write", "actions": "write"},
    )
    assert rendered(text) == [
        "actions: write # auto-resolve.yaml: land",
        "contents: write # auto-resolve.yaml: land",
        "issues: write # auto-resolve.yaml: resolve",
    ]


def test_read_survives_when_no_job_asks_for_write() -> None:
    text = callee(resolve={"contents": "read"}, land={"contents": "read"})
    assert rendered(text) == ["contents: read # auto-resolve.yaml: resolve, land"]


def test_callee_job_gaining_a_scope_reaches_the_caller() -> None:
    before = callee(resolve={"contents": "read"}, land={"contents": "write"})
    after = callee(
        resolve={"contents": "read", "packages": "write"},
        land={"contents": "write"},
    )
    assert "packages" not in " ".join(rendered(before))
    assert rendered(after) == [
        "contents: write # auto-resolve.yaml: land",
        "packages: write # auto-resolve.yaml: resolve",
    ]


def test_scopes_render_sorted_whatever_order_the_callee_declares() -> None:
    text = callee(land={"statuses": "write", "actions": "write", "issues": "write"})
    assert [line.split(":")[0] for line in rendered(text)] == [
        "actions",
        "issues",
        "statuses",
    ]


def test_a_none_only_callee_is_a_refusal_not_an_empty_mapping() -> None:
    with pytest.raises(ValueError, match="empty mapping"):
        rendered(callee(land={"contents": "none"}))


def test_string_permissions_are_refused_rather_than_expanded() -> None:
    text = "on:\n  workflow_call:\njobs:\n  land:\n    permissions: write-all\n"
    with pytest.raises(ValueError, match="scope-by-scope mapping"):
        rendered(text)


def test_unknown_level_is_refused() -> None:
    with pytest.raises(ValueError, match="none of"):
        rendered(callee(land={"contents": "admin"}))


def test_callee_without_workflow_call_is_refused() -> None:
    text = "on:\n  push:\njobs:\n  land:\n    permissions:\n      contents: write\n"
    with pytest.raises(ValueError, match="workflow_call"):
        rendered(text)


def test_callee_with_no_declared_permissions_is_refused() -> None:
    text = "on:\n  workflow_call:\njobs:\n  land:\n    runs-on: ubuntu-latest\n"
    with pytest.raises(ValueError, match="no job declares"):
        rendered(text)


def test_missing_marker_raises_instead_of_writing_nothing() -> None:
    with pytest.raises(ValueError, match="begin marker not found"):
        mod.render_caller(CALL, "jobs:\n  resolve:\n", callee(land={"issues": "write"}))


def test_real_tree_round_trips_and_is_idempotent() -> None:
    committed = CALL.caller.read_text(encoding="utf-8")
    callee_text = CALL.callee.read_text(encoding="utf-8")
    once = mod.render_caller(CALL, committed, callee_text)
    assert once == committed
    assert mod.render_caller(CALL, once, callee_text) == once


def test_real_ceiling_covers_every_scope_the_real_callee_asks_for() -> None:
    """The property a `startup_failure` violates: no callee job outranks the caller."""
    import yaml  # noqa: PLC0415  (only this test reads the tree as data)

    caller_doc = yaml.safe_load(CALL.caller.read_text(encoding="utf-8"))
    ceiling = caller_doc["jobs"]["resolve"]["permissions"]
    per_job = mod.job_permissions(
        yaml.safe_load(CALL.callee.read_text(encoding="utf-8")), CALL.callee
    )
    for job_id, block in per_job.items():
        for scope, level in block.items():
            held = mod.LEVELS[ceiling.get(scope, "none")]
            assert held >= mod.LEVELS[level], f"{job_id} outranks the caller on {scope}"
