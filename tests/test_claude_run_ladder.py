"""Behavioral tests for the credential ladder in .github/actions/claude-run.

Two properties of the composite are load-bearing and neither is visible to any
other check.

The model guard: the composite renders `--model ${{ inputs.model }}` into
claude_args on every rung. An EMPTY `model` renders the flag with no value, and
claude-code-action then silently runs on its own default. The guard step is the
choke point every caller reaches the action through.

The ladder itself: which rungs fire, given which tokens are configured and which
credentials work. A job whose middle-tier secret is unset must still reach the
tiers below it. One rung is a repeat rather than a new tier: with its own token
unset it re-spends its predecessor's credential, and only when that
predecessor's failure billed nothing. The steps are GENERATED from
.github/resolver/lib_credential_ladder.py, so these cases judge what that table
renders, by executing the action's real shell bodies and evaluating its real
`if:` expressions against a simulated step context — never by asserting the file
contains some string.

# covers: .github/actions/claude-run/action.yaml
"""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests._helpers import REPO_ROOT

ACTION = REPO_ROOT / ".github" / "actions" / "claude-run" / "action.yaml"
GUARD_STEP = "Refuse an unpinned model"


def _action() -> dict[str, Any]:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def _steps() -> list[dict[str, Any]]:
    return _action()["runs"]["steps"]


def _token_inputs() -> tuple[str, ...]:
    """The credential inputs in ladder order, read off the action's own inputs.

    Derived rather than listed so a rung added to the generator's table is
    covered by every ladder test below the moment it is rendered, instead of
    passing them by being invisible.
    """
    return tuple(name for name in _action()["inputs"] if _RUNG_INPUT.match(name))


_RUNG_INPUT = re.compile(r"^rung_(\d+)$")
_ATTEMPT_ID = re.compile(r"^a\d+$")
_CHECK_ID = re.compile(r"^c\d+$")
_STATE_ID = re.compile(r"^s\d+$")
_CLAUDE_CODE_ACTION = "anthropics/claude-code-action@"

TOKEN_INPUTS = _token_inputs()
# Rung numbers count from ONE, matching every rendered id and message.
RUNGS = range(1, len(TOKEN_INPUTS) + 1)


def _repeat_rung() -> int:
    """The rung that may re-spend its predecessor's credential, READ from the
    action: which rung repeats is a design choice the rendered ladder owns.

    A repeat rung is the one admitted by a zero-cost proof as well as by its own
    token, so its gate is the only one naming `zero_cost`.
    """
    repeats = [
        int(step_id[1:])
        for step in _steps()
        if _ATTEMPT_ID.match(step_id := str(step.get("id", "")))
        and "zero_cost" in str(step.get("if", ""))
    ]
    assert len(repeats) == 1, (
        f"expected exactly one rung admitted by a zero-cost proof, found {repeats} — "
        "a caller holding one credential gets its only retry from that rung"
    )
    return repeats[0]


REPEAT_RUNG = _repeat_rung()


def _attempt_ids() -> list[str]:
    return [str(s["id"]) for s in _steps() if _ATTEMPT_ID.match(str(s.get("id", "")))]


def test_every_credential_input_has_a_rung_that_can_spend_it() -> None:
    """A token input with no attempt step is a secret the ladder never reaches."""
    assert _attempt_ids() == [f"a{rung}" for rung in RUNGS]


def test_the_repeat_rung_falls_back_to_its_predecessors_credential_and_no_other() -> (
    None
):
    """A fallback wired to any other token input is neither same-credential nor
    free — it would spend another secret on a failure the predecessor might still
    serve, and a zero-cost proof about one credential says nothing about
    another's bill. Which input a step is wired to is invisible to the simulation
    below (no runner resolves the `with:` block), so it is read off the rendered
    `with:` keys."""
    by_id = {str(s.get("id", "")): s for s in _steps()}
    credentials = {
        key: value
        for key, value in by_id[f"a{REPEAT_RUNG}"]["with"].items()
        if key in {"anthropic_api_key", "claude_code_oauth_token"}
    }
    own, prior = f"inputs.rung_{REPEAT_RUNG}", f"inputs.rung_{REPEAT_RUNG - 1}"
    referenced = {
        reference
        for value in credentials.values()
        for reference in re.findall(r"inputs\.rung_\d+", str(value))
    }
    assert referenced == {own, prior}, credentials
    fallback = next(v for v in credentials.values() if prior in str(v))
    assert f"{own} == ''" in str(fallback), (
        "the predecessor's credential must reach the action only while this rung's "
        f"own token is unset, or a configured {own} is spent twice"
    )


def _guard_body() -> str:
    """The guard step's real `run:` body, read from the action definition."""
    guard = next((s for s in _steps() if s.get("name") == GUARD_STEP), None)
    assert guard is not None, (
        f"no {GUARD_STEP!r} step — an empty model would reach claude-code-action "
        "and it would run on its own default"
    )
    assert guard["env"]["MODEL"] == "${{ inputs.model }}"
    return guard["run"]


def _run_guard(model: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", _guard_body()],
        env={"PATH": "/usr/bin:/bin", "MODEL": model},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_guard_runs_before_any_credential_attempt() -> None:
    """A guard that ran after the first attempt would refuse an unpinned model
    only once the unpinned run had already been spent."""
    names = [str(s.get("name", "")) for s in _steps()]
    first_attempt = next(
        i
        for i, s in enumerate(_steps())
        if str(s.get("uses", "")).startswith(_CLAUDE_CODE_ACTION)
    )
    assert names.index(GUARD_STEP) < first_attempt


@pytest.mark.parametrize("model", ["", " ", "\t", "\n", "   \n  "])
def test_an_empty_model_is_refused(model: str) -> None:
    """The failure direction. Whitespace counts as empty because `--model`
    followed by blanks renders the same valueless flag an unset input does, and
    an unset action input arrives as the empty string rather than as an absent
    variable."""
    proc = _run_guard(model)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "model input is empty" in proc.stderr


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-haiku-4-5"])
def test_a_pinned_model_passes(model: str) -> None:
    """The non-vacuity pair: the guard must not red every caller, or it would be
    disabled rather than obeyed."""
    proc = _run_guard(model)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stderr == ""


# --- The ladder ------------------------------------------------------------
#
# A GitHub Actions runner is not available here, so the ladder is exercised by
# evaluating the action's own `if:` expressions and running its own state-step
# shell against a simulated step context: the attempts are stubbed (a credential
# either works or does not), every decision between them is the real thing.

_EXPR = re.compile(r"^\$\{\{(?P<body>.*)\}\}$", re.DOTALL)
_TERM = re.compile(
    r"^(?P<lhs>steps\.[A-Za-z0-9_]+\.outputs\.[A-Za-z0-9_]+|inputs\.[A-Za-z0-9_]+)"
    r"\s*(?P<op>==|!=)\s*'(?P<rhs>[^']*)'$"
)


def _read(reference: str, ctx: dict[str, Any]) -> str:
    """Resolve one `steps.<id>.outputs.<name>` / `inputs.<name>` reference.

    An unresolvable reference is the empty string, matching the runner: a
    skipped step's outputs are absent, which is exactly the state the ladder's
    gap handling turns on.
    """
    parts = reference.split(".")
    if parts[0] == "inputs":
        return str(ctx["inputs"].get(parts[1], ""))
    return str(ctx["steps"].get(parts[1], {}).get(parts[3], ""))


def _split_top(text: str, operator: str) -> list[str]:
    """Split TEXT on OPERATOR, ignoring an occurrence inside parentheses."""
    parts, depth, start = [], 0, 0
    for index, character in enumerate(text):
        depth += (character == "(") - (character == ")")
        if depth == 0 and text.startswith(operator, index):
            parts.append(text[start:index])
            start = index + len(operator)
    parts.append(text[start:])
    return parts


def _evaluate_comparison(term: str, ctx: dict[str, Any]) -> bool:
    match = _TERM.match(term.strip())
    assert match is not None, f"unsupported expression term: {term.strip()!r}"
    matches = _read(match["lhs"], ctx) == match["rhs"]
    return matches if match["op"] == "==" else not matches


def _evaluate_condition(condition: str, ctx: dict[str, Any]) -> bool:
    """Evaluate an `if:` from the grammar the ladder is allowed to use.

    That grammar is a conjunction of comparisons against string literals, where
    one conjunct may be a parenthesised disjunction of them — the boring shape
    credential-handling gates are held to. Anything richer raises instead of
    quietly evaluating, so a clever expression fails this suite rather than
    shipping unverified.
    """
    for conjunct in _split_top(" ".join(condition.split()), "&&"):
        conjunct = conjunct.strip()
        if conjunct.startswith("(") and conjunct.endswith(")"):
            alternatives = _split_top(conjunct[1:-1], "||")
            if not any(_evaluate_comparison(a, ctx) for a in alternatives):
                return False
            continue
        if not _evaluate_comparison(conjunct, ctx):
            return False
    return True


def _resolve_value(value: str, ctx: dict[str, Any]) -> str:
    """Resolve a step `env:` value: a literal, or one `${{ … }}` reference."""
    expr = _EXPR.match(str(value))
    if expr is None:
        return str(value)
    return _read(expr["body"].strip(), ctx)


def _resolve_coalesce(value: str, ctx: dict[str, Any]) -> str:
    """Resolve a `${{ a || b || … }}` chain: first non-empty reference wins."""
    expr = _EXPR.match(value.strip())
    assert expr is not None, f"not an expression: {value!r}"
    for alternative in expr["body"].split("||"):
        resolved = _read(alternative.strip(), ctx)
        if resolved:
            return resolved
    return ""


def _run_state_step(
    step: dict[str, Any], ctx: dict[str, Any], out: Path
) -> dict[str, str]:
    """Execute a ladder-state step's real `run:` body and collect its outputs."""
    env = {
        "PATH": "/usr/bin:/bin",
        "GITHUB_OUTPUT": str(out),
        **{k: _resolve_value(v, ctx) for k, v in step.get("env", {}).items()},
    }
    out.write_text("", encoding="utf-8")
    proc = subprocess.run(
        ["bash", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    written = out.read_text(encoding="utf-8").strip().splitlines()
    return dict(line.split("=", 1) for line in written if line)


class LadderRun:
    """What the ladder did: which attempts it spent, and what it reported back."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.backoffs: list[int] = []
        self.errored = ""
        self.execution_file = ""


def _simulate(
    tmp_path: Path,
    *,
    configured: set[int],
    working: set[int] = frozenset(),
    zero_billed: bool = False,
) -> LadderRun:
    """Run the ladder with `configured` tokens present and `working` ones alive.

    `zero_billed` is what claude-run-errored.sh reports for every attempt of
    this run: true models the failure that never reached inference (a provider
    blip, a dead token), false the one that billed real money and then failed on
    the work. A rung in `working` succeeds even when the rung before it failed on
    the same credential — that is the whole premise of the repeat rung.
    """
    inputs = dict.fromkeys(TOKEN_INPUTS, "")
    for rung in configured:
        inputs[f"rung_{rung}"] = f"token-{rung}"
    inputs |= {
        "model": "claude-sonnet-5",
        "prompt": "p",
        "claude_args": "",
        "github_token": "t",
        "gate_execution": "false",
    }
    ctx: dict[str, Any] = {"inputs": inputs, "steps": {}}
    succeeds = {f"a{rung}" for rung in working}
    run = LadderRun()
    out = tmp_path / "github_output"

    for step in _steps():
        if "if" in step and not _evaluate_condition(step["if"], ctx):
            continue
        step_id = str(step.get("id", ""))
        if _ATTEMPT_ID.match(step_id):
            run.attempts.append(step_id)
            ctx["steps"][step_id] = {"execution_file": f"execution-{step_id}.json"}
        elif _CHECK_ID.match(step_id):
            # claude-run-errored.sh's verdict for this attempt, from the same
            # input it reads: an attempt that never wrote a log is errored.
            # zero_cost is reported independently of errored — a healthy run can
            # bill nothing — so a gate that read it alone would retry a
            # successful attempt.
            attempt = f"a{step_id[1:]}"
            ran = attempt in ctx["steps"]
            ctx["steps"][step_id] = {
                "errored": "false" if (ran and attempt in succeeds) else "true",
                "zero_cost": "true" if zero_billed else "false",
            }
        elif _STATE_ID.match(step_id):
            ctx["steps"][step_id] = _run_state_step(step, ctx, out)
        elif step_id == "resolve_log":
            ctx["steps"][step_id] = {
                "execution_file": _resolve_coalesce(step["env"]["EXEC_FILE"], ctx)
            }
        elif str(step.get("name", "")).startswith("Back off"):
            run.backoffs.append(int(step["env"]["BACKOFF_SECONDS"]))
        elif str(step.get("name", "")) == "Propagate failover result":
            # The composite's only failure verdict: the propagate step fires
            # exactly when no attempt produced a non-errored run.
            run.errored = "true"

    run.errored = run.errored or "false"
    run.execution_file = _read("steps.resolve_log.outputs.execution_file", ctx)
    return run


def test_a_gap_in_the_token_list_does_not_truncate_the_ladder(tmp_path: Path) -> None:
    """Tiers 3, 5 and 6 hold live secrets while tier 4 is unset. Gating each rung
    on its immediate predecessor's check makes the unset tier's SKIPPED check the
    terminator, so tiers 5 and 6 never fire and the job dies holding two unspent
    credentials."""
    run = _simulate(tmp_path, configured={1, 3, 5, 6})
    assert run.attempts == ["a1", "a3", "a5", "a6"]


def test_the_free_retry_does_not_swallow_the_ladders_failure(tmp_path: Path) -> None:
    """The repeat rung joins the cumulative state rather than sitting beside it,
    so a run whose every attempt failed still reaches the propagate gate. A
    repeat that reported its own outcome as an answer would turn an exhausted
    ladder green — the one outcome the propagate step exists to make loud."""
    run = _simulate(tmp_path, configured={1, 3, 5, 6}, zero_billed=True)
    assert run.attempts == ["a1", "a2", "a3", "a5", "a6"]
    assert run.errored == "true"


def test_a_setup_holding_only_the_last_rung_pays_for_no_earlier_tier(
    tmp_path: Path,
) -> None:
    """The minimal setup: one token, in the last rung. Every earlier tier is
    unset, and an unset tier must be SKIPPED rather than attempted — an
    unconditional first rung spends a dead invocation plus a backoff before the
    ladder reaches the only credential the caller actually has."""
    run = _simulate(tmp_path, configured={max(RUNGS)})
    assert run.attempts == [f"a{max(RUNGS)}"]
    assert run.backoffs == [], "an unset tier must cost no wait either"


def test_every_configured_rung_is_spent_before_the_ladder_gives_up(
    tmp_path: Path,
) -> None:
    run = _simulate(tmp_path, configured=set(RUNGS))
    assert run.attempts == [f"a{rung}" for rung in RUNGS]


@pytest.mark.parametrize("gap", list(RUNGS)[1:-1])
def test_each_single_unset_tier_is_stepped_over(tmp_path: Path, gap: int) -> None:
    """Member-by-member over the tiers that can be a gap: rung 1 has no
    predecessor to strand it and the last rung has no successor to strand."""
    configured = set(RUNGS) - {gap}
    run = _simulate(tmp_path, configured=configured)
    assert run.attempts == [f"a{rung}" for rung in sorted(configured)]


@pytest.mark.parametrize("winner", list(RUNGS))
def test_the_ladder_stops_at_the_first_credential_that_works(
    tmp_path: Path, winner: int
) -> None:
    """The non-vacuity pair for the gap tests: a ladder that ignored `errored`
    entirely would also pass them, while burning every token on every run."""
    run = _simulate(tmp_path, configured=set(RUNGS), working={winner})
    assert run.attempts == [f"a{rung}" for rung in range(min(RUNGS), winner + 1)]
    assert run.errored == "false"
    assert run.execution_file == f"execution-a{winner}.json"


def test_an_exhausted_ladder_fails_rather_than_reporting_success(
    tmp_path: Path,
) -> None:
    """The composite must never hand a caller a green verdict for a run no
    credential completed — the propagate step is the caller's only failure
    signal."""
    run = _simulate(tmp_path, configured=set(RUNGS))
    assert run.errored == "true"
    assert run.execution_file == f"execution-a{max(RUNGS)}.json"


def test_a_lone_dead_primary_still_fails(tmp_path: Path) -> None:
    """No fallback is configured and the failure billed, so no retry gate can be
    true; the primary's check is ungated precisely so the verdict is never empty
    here."""
    run = _simulate(tmp_path, configured={1})
    assert run.attempts == ["a1"]
    assert run.errored == "true"


# --- The free same-credential retry ----------------------------------------


@pytest.mark.parametrize(
    ("zero_billed", "expected"),
    [
        (True, [f"a{REPEAT_RUNG - 1}", f"a{REPEAT_RUNG}"]),
        (False, [f"a{REPEAT_RUNG - 1}"]),
    ],
)
def test_only_a_zero_billed_failure_earns_a_free_same_credential_retry(
    tmp_path: Path, zero_billed: bool, expected: list[str]
) -> None:
    """Both directions of the rung's whole reason to exist, on the configuration
    that has no other retry available at all — the primary token alone.

    zero_billed: the failure never reached inference, so the blip and the dead
    token are indistinguishable and one repeat costs nothing either way. Without
    this rung the ladder answers a blip by walking to a credential this caller
    does not have, and the attempt is lost with ZERO retries.

    not zero_billed: the run reached inference and failed on the work, so a
    repeat on the same credential would spend real money to fail the same way.
    """
    run = _simulate(tmp_path, configured={REPEAT_RUNG - 1}, zero_billed=zero_billed)
    assert run.attempts == expected


@pytest.mark.parametrize("zero_billed", [True, False])
def test_a_non_errored_run_is_never_retried(tmp_path: Path, zero_billed: bool) -> None:
    """zero_cost is orthogonal to errored — a successful run that happens to
    bill nothing still reports zero_cost=true — so a gate reading it alone would
    repeat a run that already delivered. The cumulative ladder state is the
    other half of the gate, and this is what proves it is still being read."""
    run = _simulate(
        tmp_path, configured=set(RUNGS), working={min(RUNGS)}, zero_billed=zero_billed
    )
    assert run.attempts == [f"a{min(RUNGS)}"]
    assert run.errored == "false"
    assert run.execution_file == f"execution-a{min(RUNGS)}.json"


@pytest.mark.parametrize("repeat_works", [True, False])
def test_the_outputs_report_the_free_retry_when_it_is_the_last_attempt(
    tmp_path: Path, repeat_works: bool
) -> None:
    """The newest-first `||` chain must place the repeat ahead of the rung it
    repeats, or the caller reads the log of a superseded attempt — a stale
    execution_file for checks/claude-execution.py to classify."""
    run = _simulate(
        tmp_path,
        configured={REPEAT_RUNG - 1},
        working={REPEAT_RUNG} if repeat_works else set(),
        zero_billed=True,
    )
    assert run.attempts == [f"a{REPEAT_RUNG - 1}", f"a{REPEAT_RUNG}"]
    assert run.errored == ("false" if repeat_works else "true")
    assert run.execution_file == f"execution-a{REPEAT_RUNG}.json"


def test_a_successful_free_retry_ends_the_ladder_green(tmp_path: Path) -> None:
    """A repeat that delivered must leave the ladder reporting success, or the
    propagate gate fails a run that actually got its answer."""
    run = _simulate(
        tmp_path,
        configured={REPEAT_RUNG - 1},
        working={REPEAT_RUNG},
        zero_billed=True,
    )
    assert run.attempts == [f"a{REPEAT_RUNG - 1}", f"a{REPEAT_RUNG}"]
    assert run.errored == "false"


def test_the_free_retry_waits_out_the_blip_before_repeating(tmp_path: Path) -> None:
    """A repeat fired back-to-back lands inside the same sub-second fault the
    first attempt hit, so the rung would answer a blip by confirming it. The
    wait is the rung's whole mechanism, and here no other credential backoff can
    supply it."""
    run = _simulate(tmp_path, configured={REPEAT_RUNG - 1}, zero_billed=True)
    assert len(run.backoffs) == 1, "the free retry fired with no wait before it"
    assert run.backoffs[0] >= 5, "a sub-5s wait does not outlast a provider blip"
    assert run.backoffs[0] <= 60, f"a free retry should not idle {run.backoffs[0]}s"


def test_each_retry_waits_and_the_total_wait_is_bounded(tmp_path: Path) -> None:
    """A credential-side rejection costs ~500ms, so an unspaced ladder is spent
    in seconds and one provider blip takes every rung. The waits must therefore
    be real and escalating — and bounded, or a dead-credential run holds a
    runner.

    Scoped to the CREDENTIAL ladder (a billed failure, so the free
    same-credential rung is skipped): that rung's own wait is a separate
    property, since it is free and so wants the shortest wait that clears a blip
    rather than an escalating one.
    """
    run = _simulate(tmp_path, configured=set(RUNGS))
    assert len(run.backoffs) == len(RUNGS) - 1
    assert all(
        earlier < later
        for earlier, later in zip(run.backoffs, run.backoffs[1:], strict=False)
    )
    assert run.backoffs[0] >= 5, "a sub-5s wait does not outlast a provider blip"
    assert sum(run.backoffs) <= 300, f"ladder adds {sum(run.backoffs)}s of wall clock"


def test_no_backoff_is_spent_once_a_credential_works(tmp_path: Path) -> None:
    """The wait is gated with its rung, so a healthy primary costs nothing."""
    run = _simulate(tmp_path, configured=set(RUNGS), working={min(RUNGS)})
    assert run.backoffs == []


def test_the_backoff_step_really_sleeps_for_its_configured_seconds(
    tmp_path: Path,
) -> None:
    """The `if:` simulation above reads the declared budget; this proves the
    body the runner executes actually invokes `sleep` with that budget,
    rather than merely carrying the number in an unreachable branch. A
    recording `sleep` stub on PATH avoids spending real wall-clock time: it
    logs its argument instead of sleeping, so a body that never calls
    `sleep` (or calls it with the wrong value) leaves the wrong evidence
    rather than merely finishing early on a fast or loaded runner."""
    step = next(s for s in _steps() if str(s.get("name", "")).startswith("Back off"))
    log = tmp_path / "sleep.log"
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "sleep").write_text(
        f'#!/bin/sh\necho "$1" >>"{log}"\n', encoding="utf-8"
    )
    (stub_dir / "sleep").chmod(0o755)
    proc = subprocess.run(
        ["bash", "-c", step["run"]],
        env={"PATH": f"{stub_dir}:/usr/bin:/bin", "BACKOFF_SECONDS": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert log.read_text(encoding="utf-8").splitlines() == ["1"], (
        "backoff body did not sleep for BACKOFF_SECONDS"
    )


def test_every_retry_rung_is_preceded_by_a_backoff_on_its_own_gate() -> None:
    """Choke-point uniformity: one unspaced rung is enough for a blip to take
    two credentials at once. A wait on a looser gate would idle a runner before
    an attempt that never happens; on a tighter one it would let a rung through
    unspaced.

    A credential rung's backoff carries ONE extra clause its attempt does not:
    the wait straddles a blip BETWEEN two attempts, so it is skipped until some
    attempt has actually run. Without it a setup holding only the last rung
    waits out every earlier tier's backoff before its single attempt."""
    steps = _steps()
    for index, step in enumerate(steps):
        rung = str(step.get("id", ""))
        if not _ATTEMPT_ID.match(rung) or rung == f"a{min(RUNGS)}":
            continue
        backoff = steps[index - 1]
        assert str(backoff.get("name", "")).startswith("Back off"), (
            f"rung {rung} retries with no preceding backoff"
        )
        attempt_gate = " ".join(str(step["if"]).split())
        backoff_gate = " ".join(str(backoff["if"]).split())
        if backoff_gate == attempt_gate:
            continue
        state = attempt_gate.split(".")[1]
        assert backoff_gate == (
            f"{attempt_gate} && steps.{state}.outputs.any_attempt == 'true'"
        ), f"rung {rung}'s backoff gate is not its attempt gate plus any_attempt"
