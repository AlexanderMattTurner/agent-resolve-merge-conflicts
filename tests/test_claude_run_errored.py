"""Behavior tests for .github/resolver/claude-run-errored.sh — the retry-decision
helper that decides whether a claude-code-action attempt failed hard enough to
retry on the next credential rung. It reads the execution log (the only signal,
since the action exits 0 even on is_error) and writes errored=true/false plus
zero_cost=true/false to GITHUB_OUTPUT; it fails the step only on no log plus an empty
GitHub allowance.

Drives the real script as a subprocess and asserts the GITHUB_OUTPUT values, with `gh`
and `sleep` stubbed on PATH.

# covers: .github/resolver/claude-run-errored.sh
"""

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT, write_exe

SCRIPT = REPO_ROOT / ".github" / "resolver" / "claude-run-errored.sh"


REFUSAL = "API rate limit exceeded for installation"


def stub_cli(
    tmp_path: Path,
    *,
    remaining: int | None = None,
    reset_in: int = 1800,
    refusal: str = "",
) -> None:
    """Stub `gh api rate_limit` (`remaining` requests, else a refusal) and a logging `sleep`."""
    core = {"limit": 5000, "remaining": remaining, "reset": int(time.time()) + reset_in}
    reply = (
        f"echo {shlex.quote(refusal or 'gh: authentication required')} >&2; exit 1"
        if remaining is None
        else f"echo {shlex.quote(json.dumps({'resources': {'core': core}}))}"
    )
    write_exe(tmp_path / "bin" / "gh", f"#!/usr/bin/env bash\n{reply}\n")
    sleep_log = tmp_path / "sleeps"
    write_exe(
        tmp_path / "bin" / "sleep", f'#!/usr/bin/env bash\necho "$1" >>"{sleep_log}"\n'
    )


def sleeps(tmp_path: Path) -> list[str]:
    log = tmp_path / "sleeps"
    return log.read_text(encoding="utf-8").split() if log.exists() else []


def decide(tmp_path: Path, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    """Run the real script with EXECUTION_FILE scrubbed, against `stub_cli`'s stubs."""
    if not (tmp_path / "bin" / "gh").exists():
        stub_cli(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "EXECUTION_FILE"}
    env["GITHUB_OUTPUT"] = str(tmp_path / "github_output")
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def run_decider(tmp_path: Path, **env_overrides: str) -> dict[str, str]:
    """Run the decider; return the recorded outputs (errored, zero_cost)."""
    proc = decide(tmp_path, **env_overrides)
    assert proc.returncode == 0, proc.stderr
    lines = (tmp_path / "github_output").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if line)


def write_log(tmp_path: Path, payload: object) -> Path:
    log = tmp_path / "execution.json"
    log.write_text(json.dumps(payload), encoding="utf-8")
    return log


def test_unset_execution_file_is_errored(tmp_path: Path) -> None:
    # No log ⇒ a crash that never reached the model: errored AND provably free.
    out = run_decider(tmp_path)
    assert out["errored"] == "true"
    assert out["zero_cost"] == "true"


def test_missing_file_is_errored(tmp_path: Path) -> None:
    out = run_decider(tmp_path, EXECUTION_FILE=str(tmp_path / "absent.json"))
    assert out["errored"] == "true"
    assert out["zero_cost"] == "true"


def test_empty_file_is_errored(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    out = run_decider(tmp_path, EXECUTION_FILE=str(empty))
    assert out["errored"] == "true"
    assert out["zero_cost"] == "true"


def test_is_error_true_zero_cost_is_errored_and_free(tmp_path: Path) -> None:
    # is_error with no billed inference: retryable, and free to retry (same token).
    log = write_log(tmp_path, {"type": "result", "is_error": True, "total_cost_usd": 0})
    out = run_decider(tmp_path, EXECUTION_FILE=str(log))
    assert out["errored"] == "true"
    assert out["zero_cost"] == "true"


def test_is_error_true_with_cost_is_errored_not_free(tmp_path: Path) -> None:
    # A run that spent real money before erroring is retryable only on a DISTINCT
    # credential — zero_cost=false so the same-token free retry does not fire.
    log = write_log(
        tmp_path, {"type": "result", "is_error": True, "total_cost_usd": 0.2}
    )
    out = run_decider(tmp_path, EXECUTION_FILE=str(log))
    assert out["errored"] == "true"
    assert out["zero_cost"] == "false"


def test_is_error_false_is_not_errored(tmp_path: Path) -> None:
    log = write_log(
        tmp_path, {"type": "result", "is_error": False, "total_cost_usd": 0.1}
    )
    out = run_decider(tmp_path, EXECUTION_FILE=str(log))
    assert out["errored"] == "false"
    assert out["zero_cost"] == "false"


def test_array_form_last_result_wins(tmp_path: Path) -> None:
    log = write_log(
        tmp_path,
        [
            {"type": "system", "subtype": "init"},
            {"type": "result", "is_error": False, "total_cost_usd": 0.1},
        ],
    )
    out = run_decider(tmp_path, EXECUTION_FILE=str(log))
    assert out["errored"] == "false"
    assert out["zero_cost"] == "false"


def test_is_error_absent_is_not_errored(tmp_path: Path) -> None:
    # A result with no is_error field is not an error run, so no retry.
    log = write_log(tmp_path, {"type": "result", "total_cost_usd": 0.5})
    out = run_decider(tmp_path, EXECUTION_FILE=str(log))
    assert out["errored"] == "false"
    assert out["zero_cost"] == "false"


def test_a_log_means_github_is_never_asked_for_its_budget(tmp_path: Path) -> None:
    stub_cli(tmp_path, remaining=0)
    log = write_log(tmp_path, {"type": "result", "is_error": True, "total_cost_usd": 0})
    assert run_decider(tmp_path, EXECUTION_FILE=str(log))["errored"] == "true"
    assert sleeps(tmp_path) == []


@pytest.mark.parametrize(
    ("stub", "said"),
    [
        ({"remaining": 0}, "core rate limit is exhausted"),
        ({"refusal": f"gh: {REFUSAL}. (HTTP 403)"}, REFUSAL),
    ],
    ids=["reset 30 min away", "refused budget read, no reset known"],
)
def test_an_empty_github_allowance_stops_the_ladder_at_once(
    tmp_path: Path, stub: dict, said: str
) -> None:
    # A refused `GET /rate_limit` gives no reset time, and each rung's fresh process would
    # restart a guessed wait: stop without sleeping.
    stub_cli(tmp_path, **stub)
    proc = decide(tmp_path)
    assert proc.returncode == 1
    assert "::error::GitHub refused this attempt before any model ran" in proc.stderr
    assert said in proc.stderr
    assert not (tmp_path / "github_output").exists()
    assert sleeps(tmp_path) == []


def test_a_reset_inside_the_allowed_wait_is_slept_out(tmp_path: Path) -> None:
    stub_cli(tmp_path, remaining=0, reset_in=30)
    assert run_decider(tmp_path)["errored"] == "true"
    (slept,) = sleeps(tmp_path)
    assert 25 <= int(slept) <= 40


@pytest.mark.parametrize("remaining", [40, None], ids=["requests left", "unreadable"])
def test_a_github_allowance_that_is_not_empty_leaves_the_ladder_running(
    tmp_path: Path, remaining: int | None
) -> None:
    stub_cli(tmp_path, remaining=remaining)
    assert run_decider(tmp_path)["errored"] == "true"
    assert sleeps(tmp_path) == []
