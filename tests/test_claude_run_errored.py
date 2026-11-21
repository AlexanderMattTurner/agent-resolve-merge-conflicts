"""Behavior tests for .github/resolver/claude-run-errored.sh — the retry-decision
helper that decides whether a claude-code-action attempt failed hard enough to
retry on the next credential rung. It reads the execution log (the only signal,
since the action exits 0 even on is_error) and writes errored=true/false plus
zero_cost=true/false to GITHUB_OUTPUT. It fails the step only when no log exists and
GitHub's own allowance is empty: no other credential changes that refusal.

Drives the real script as a subprocess and asserts the GITHUB_OUTPUT values. The one
external dependency, `gh`, is a stub on PATH that replays `GET /rate_limit`'s real
document shape: `tests/test_gh_rate_limit.py` already reads a real `gh` against a real
HTTPS server, so what this file adds is the decider's choice between the next
credential and a stop. This file also runs on the macOS leg, where that server cannot.

# covers: .github/resolver/claude-run-errored.sh
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT, write_exe

SCRIPT = REPO_ROOT / ".github" / "resolver" / "claude-run-errored.sh"

# GitHub's own words for the primary limit on an installation token, which is what
# `github.token` is. Copied from the failing step of the 2026-11-11 merge-delta review.
REFUSAL = "API rate limit exceeded for installation"

# Answers `gh api rate_limit` the way the endpoint does: a document, or a refusal on
# stderr with a non-zero exit. Any other call fails loud, so a decider that starts
# asking `gh` something else cannot pass by accident.
_GH_STUB = """#!/usr/bin/env bash
if [[ "$1 $2" != "api rate_limit" ]]; then
  echo "unexpected gh call: $*" >&2
  exit 2
fi
echo "$*" >>"$STUB_DIR/gh-calls"
if [[ -s "$STUB_DIR/gh-refusal" ]]; then
  cat "$STUB_DIR/gh-refusal" >&2
  exit 1
fi
cat "$STUB_DIR/gh-body"
"""

# Records the seconds it is asked to wait instead of waiting them.
_SLEEP_STUB = """#!/usr/bin/env bash
echo "$1" >>"$STUB_DIR/sleeps"
"""


class Stubs:
    """The `gh` and `sleep` the decider meets, and what each was asked."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path / "stubs"
        self.dir.mkdir()
        write_exe(self.dir / "gh", _GH_STUB)
        write_exe(self.dir / "sleep", _SLEEP_STUB)
        self.unreadable()

    def budget(self, *, remaining: int, reset_in: int) -> None:
        """`GET /rate_limit` answers with this core bucket."""
        core = {
            "limit": 5000,
            "remaining": remaining,
            "reset": int(time.time()) + reset_in,
        }
        graphql = {**core, "remaining": 5000}
        document = {"resources": {"core": core, "graphql": graphql}}
        (self.dir / "gh-body").write_text(json.dumps(document), encoding="utf-8")
        (self.dir / "gh-refusal").write_text("", encoding="utf-8")

    def refused(self) -> None:
        """An empty budget refuses `GET /rate_limit` itself, so no bucket is readable."""
        (self.dir / "gh-refusal").write_text(
            f"gh: {REFUSAL}. (HTTP 403)\n", encoding="utf-8"
        )

    def unreadable(self) -> None:
        """`gh` fails for a reason that is not a rate limit, such as no token."""
        (self.dir / "gh-refusal").write_text(
            "gh: authentication required\n", encoding="utf-8"
        )

    def sleeps(self) -> list[str]:
        path = self.dir / "sleeps"
        return path.read_text(encoding="utf-8").split() if path.exists() else []

    def gh_calls(self) -> int:
        path = self.dir / "gh-calls"
        if not path.exists():
            return 0
        return len(path.read_text(encoding="utf-8").splitlines())


def decide(
    tmp_path: Path, stubs: Stubs | None = None, **env_overrides: str
) -> subprocess.CompletedProcess[str]:
    """Run the real script with a fresh GITHUB_OUTPUT and EXECUTION_FILE scrubbed
    from the inherited env, against STUBS (an unreadable `gh` unless given)."""
    stubs = stubs or Stubs(tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "EXECUTION_FILE"}
    env.update(
        GITHUB_OUTPUT=str(tmp_path / "github_output"),
        STUB_DIR=str(stubs.dir),
        PATH=f"{stubs.dir}{os.pathsep}{env['PATH']}",
    )
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def outputs_of(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "github_output"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return dict(line.split("=", 1) for line in text.splitlines() if line)


def run_decider(tmp_path: Path, **env_overrides: str) -> dict[str, str]:
    """Run the decider; return the recorded outputs (errored, zero_cost)."""
    proc = decide(tmp_path, **env_overrides)
    assert proc.returncode == 0, proc.stderr
    return outputs_of(tmp_path)


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


def test_a_dead_credential_with_a_log_never_asks_github_for_its_budget(
    tmp_path: Path,
) -> None:
    # A log is evidence about the credential. Only a missing one needs the question.
    stubs = Stubs(tmp_path)
    stubs.budget(remaining=0, reset_in=1800)
    log = write_log(tmp_path, {"type": "result", "is_error": True, "total_cost_usd": 0})
    proc = decide(tmp_path, stubs, EXECUTION_FILE=str(log))
    assert proc.returncode == 0, proc.stderr
    assert outputs_of(tmp_path)["errored"] == "true"
    assert stubs.gh_calls() == 0


def test_an_empty_github_allowance_stops_the_ladder_with_github_s_error(
    tmp_path: Path,
) -> None:
    # 2026-11-11, merge-delta review: GitHub refused both rungs for the installation's
    # budget, and each was reported as a dead credential. Another rung cannot help.
    stubs = Stubs(tmp_path)
    stubs.budget(remaining=0, reset_in=1800)
    proc = decide(tmp_path, stubs)
    assert proc.returncode == 1
    assert "::error::GitHub refused this attempt before any model ran" in proc.stderr
    assert "core rate limit is exhausted" in proc.stderr
    # No verdict at all, so no later step reads `errored=true` as a dead credential.
    assert outputs_of(tmp_path) == {}
    assert stubs.sleeps() == []


def test_a_reset_inside_the_allowed_wait_is_slept_out_and_the_ladder_goes_on(
    tmp_path: Path,
) -> None:
    stubs = Stubs(tmp_path)
    stubs.budget(remaining=0, reset_in=30)
    proc = decide(tmp_path, stubs)
    assert proc.returncode == 0, proc.stderr
    assert outputs_of(tmp_path)["errored"] == "true"
    (slept,) = stubs.sleeps()
    assert 25 <= int(slept) <= 40
    assert "waiting" in proc.stderr


def test_a_refusal_that_hides_every_bucket_still_counts_as_github_s(
    tmp_path: Path,
) -> None:
    # The budget that refuses the action refuses `GET /rate_limit` too, so no bucket
    # reads empty. GitHub's own refusal is then the only evidence, and with no waiting
    # budget left the ladder stops on it.
    stubs = Stubs(tmp_path)
    stubs.refused()
    proc = decide(tmp_path, stubs, GH_RATE_LIMIT_BLIND_TOTAL_SECS="0")
    assert proc.returncode == 1
    assert "::error::GitHub refused this attempt before any model ran" in proc.stderr
    assert "refused this call for rate limiting" in proc.stderr
    assert outputs_of(tmp_path) == {}


def test_a_refusal_that_hides_every_bucket_is_waited_out_within_the_budget(
    tmp_path: Path,
) -> None:
    stubs = Stubs(tmp_path)
    stubs.refused()
    proc = decide(tmp_path, stubs)
    assert proc.returncode == 0, proc.stderr
    assert outputs_of(tmp_path)["errored"] == "true"
    (slept,) = stubs.sleeps()
    assert 45 <= int(slept) <= 75


@pytest.mark.parametrize("reading", ["with requests left", "unreadable"])
def test_a_github_allowance_that_is_not_empty_leaves_the_ladder_running(
    tmp_path: Path, reading: str
) -> None:
    stubs = Stubs(tmp_path)
    if reading == "with requests left":
        stubs.budget(remaining=40, reset_in=1800)
    proc = decide(tmp_path, stubs)
    assert proc.returncode == 0, proc.stderr
    out = outputs_of(tmp_path)
    assert (out["errored"], out["zero_cost"]) == ("true", "true")
    assert stubs.sleeps() == []
