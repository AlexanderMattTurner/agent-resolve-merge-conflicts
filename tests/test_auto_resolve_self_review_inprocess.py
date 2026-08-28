"""In-process tests for .github/resolver/auto-resolve/self_review.py.

The suite beside this one drives the real entry point as a CHILD process, which is
what proves the gate's contract — and which coverage cannot trace into. These cases
import the module and call its functions, so the reader's own decisions (jq's falsy
set, the workflow-command escaping, the byte cap, the ladder walk) are measured as
well as asserted.
"""

# covers: .github/resolver/auto-resolve/self_review.py

import json
import os
from pathlib import Path

import pytest

from tests._helpers import commit_files, init_test_repo
from tests._resolver_helpers import REPO_ROOT, load_script
from tests.test_auto_resolve_self_review import (
    FAKE_CLAUDE,
    LADDER_VARS,
    RESOLUTION_FULLY_RETIRED,
    RESOLUTION_WITH_DELTA,
    git_in,
    repo_with_resolved_merge,
)

sr = load_script(".github/resolver/auto-resolve/self_review.py")

CLEAN = (
    "No suspicious merge-resolution deltas: every hand-authored change traces to a "
    "parent's intent.\n"
)


def _config(tmp_path: Path, repo: Path, **over):
    """A SelfReviewConfig built without an environment read, for the units below."""
    fields = {
        "repo": repo,
        "base_worktree": REPO_ROOT,
        "review_dir": tmp_path / "sr",
        "max_rounds": 1,
        "budget_seconds": 3600,
        "timeout_seconds": 30,
        "ladder": ("cred-1",),
    }
    fields.update(over)
    Path(str(fields["review_dir"])).mkdir(parents=True, exist_ok=True)
    return sr.SelfReviewConfig(**fields)


# --------------------------------------------------------------- jq's semantics


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "fb"), (False, "fb"), ("", ""), (0, 0), ({}, {}), ("x", "x")],
)
def test_the_fallback_drops_exactly_jqs_falsy_set(
    value: object, expected: object
) -> None:
    """The script was written against jq's `//`, whose falsy set is exactly
    {null, false}. A Python `or` also drops "", {} and 0, so a run that reported
    status 0 or an empty message would be described by the fallback instead of by
    itself."""
    assert sr._coalesce(value, "fb") == expected


@pytest.mark.parametrize(
    ("value", "expected"), [("x", "x"), (401, "401"), (True, "true"), (None, "null")]
)
def test_interpolation_renders_a_string_raw_and_anything_else_as_json(
    value: object, expected: str
) -> None:
    """jq's `\\(…)` inserts a string's own characters and any other value's JSON
    form, so a numeric status reaches the log as `401` rather than as `"401"`."""
    assert sr._jq_interpolate(value) == expected


# ---------------------------------------------------------------- the model log


def test_an_absent_or_unparseable_log_reads_as_no_verdict(tmp_path: Path) -> None:
    assert sr._read_log(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert sr._read_log(bad) is None


@pytest.mark.parametrize("body", ["null", "false"])
def test_a_log_the_shell_refused_still_reads_as_no_verdict(
    tmp_path: Path, body: str
) -> None:
    """`jq -e .` exits 1 on a document that is literally null or false, so those are
    runs with no verdict rather than logs to read a field out of."""
    log = tmp_path / "log.json"
    log.write_text(body, encoding="utf-8")
    assert sr._read_log(log) is None


def test_a_zero_valued_log_is_not_swept_into_that_refusal(tmp_path: Path) -> None:
    """The other side of the same line: `0` is truthy to jq, so a `not data` test
    would have refused a log the shell accepted."""
    log = tmp_path / "log.json"
    log.write_text("0", encoding="utf-8")
    assert sr._read_log(log) == 0


def test_the_run_cause_is_silent_when_the_log_names_no_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "log.json"
    log.write_text(json.dumps({"is_error": True}), encoding="utf-8")
    sr._report_run_cause(log)
    log.write_text("[]", encoding="utf-8")
    sr._report_run_cause(log)
    assert capsys.readouterr().err == ""


def test_the_run_cause_escapes_a_line_the_runner_would_execute(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The text is the model's own output, and a line beginning `::` is a workflow
    command the runner executes rather than prints."""
    log = tmp_path / "log.json"
    log.write_text(
        json.dumps({"api_error_status": 401, "result": "Invalid\n::stop-commands::x"}),
        encoding="utf-8",
    )
    sr._report_run_cause(log)
    err = capsys.readouterr().err
    assert "status 401: Invalid" in err
    assert " ::stop-commands::x\n" in err
    assert "\n::stop-commands::x" not in err


def test_the_run_cause_names_whichever_half_the_log_carries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "log.json"
    log.write_text(json.dumps({"result": "over quota"}), encoding="utf-8")
    sr._report_run_cause(log)
    assert "status none: over quota" in capsys.readouterr().err


def test_the_run_cause_is_capped_so_a_model_cannot_flood_the_step_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "log.json"
    log.write_text(json.dumps({"result": "z" * 9000}), encoding="utf-8")
    sr._report_run_cause(log)
    # The cap is on the bytes read out of the log; the escape pass may add one
    # space per line to what survives it.
    assert len(capsys.readouterr().err.encode("utf-8")) <= 4097


# ------------------------------------------------------- the called bash library


def test_the_verdict_predicate_is_the_shared_bash_one(tmp_path: Path) -> None:
    """Called out of lib/merge-delta-verdict.bash rather than reimplemented, so a
    quoted all-clear is refused here for the same reason the PR-side gate refuses
    it."""
    review = tmp_path / "merge-review.md"
    review.write_text(CLEAN, encoding="utf-8")
    assert sr.review_is_clean(review)
    review.write_text("> " + CLEAN, encoding="utf-8")
    assert not sr.review_is_clean(review)


def test_the_ladder_reads_its_values_from_this_processes_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """oauth_ladder_names decides which rungs survive and emits NAMES, so no token
    crosses a pipe. An unset middle rung is stepped over, not truncated."""
    assert LADDER_VARS, "read no ladder rungs — every case below would pass over none"
    for name in LADDER_VARS:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv(LADDER_VARS[0], "first")
    monkeypatch.setenv(LADDER_VARS[-1], "last")
    assert sr.oauth_ladder() == ["first", "last"]


def test_a_ladder_that_could_not_be_read_is_cannot_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ladder nothing could read is not an empty ladder: reporting it as one says
    "no credential is configured" for a library that simply failed to load."""
    monkeypatch.setattr(sr, "_LIB", tmp_path)
    with pytest.raises(SystemExit) as caught:
        sr.oauth_ladder()
    assert caught.value.code == sr._EXIT_CANNOT_VERIFY


@pytest.mark.parametrize(
    ("credential", "metered"), [("sk-ant-oat-x", False), ("sk-ant-api-x", True)]
)
def test_the_credential_shape_test_is_the_shared_bash_one(
    credential: str, metered: bool
) -> None:
    assert sr._is_metered(credential) is metered


# ------------------------------------------------------------------- the config


def test_a_run_without_the_trusted_base_worktree_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("BASE_WORKTREE", raising=False)
    with pytest.raises(SystemExit) as caught:
        sr.SelfReviewConfig.from_env(tmp_path)
    assert "BASE_WORKTREE" in str(caught.value)


def test_an_absent_tunable_falls_back_and_the_passed_ordering_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """bundle.py passes the ordering it already proved, so review and hook repair
    spend the same rung rather than re-paying for a dead one — and the scratch
    directory defaults under the runner's own temp."""
    monkeypatch.setenv("BASE_WORKTREE", str(REPO_ROOT))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.delenv("SELF_REVIEW_DIR", raising=False)
    monkeypatch.delenv("MERGE_DELTA_MAX_ROUNDS", raising=False)
    monkeypatch.delenv("SELF_REVIEW_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("SELF_REVIEW_TOKEN_LADDER", "a\nb")
    cfg = sr.SelfReviewConfig.from_env(tmp_path)
    assert cfg.review_dir == tmp_path / "self-review"
    assert cfg.review_dir.is_dir(), "the scratch directory is created, not assumed"
    assert cfg.ladder == ("a", "b")
    assert cfg.max_rounds == 2, "two fix rounds by default"
    # The helper ships WITH the resolver, so its path comes from the resolver
    # checkout rather than from BASE_WORKTREE (the tree under review).
    assert cfg.script("x.py") == str(REPO_ROOT / ".github" / "resolver" / "x.py")


def test_the_configured_ladder_is_walked_when_no_ordering_is_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BASE_WORKTREE", str(REPO_ROOT))
    monkeypatch.setenv("SELF_REVIEW_DIR", str(tmp_path / "sr"))
    monkeypatch.delenv("SELF_REVIEW_TOKEN_LADDER", raising=False)
    monkeypatch.setenv("MERGE_DELTA_MAX_ROUNDS", "3")
    for name in LADDER_VARS:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv(LADDER_VARS[1], "only-one")
    cfg = sr.SelfReviewConfig.from_env(tmp_path)
    assert cfg.ladder == ("only-one",)
    assert cfg.max_rounds == 3


# ------------------------------------------------------------ the bounded runner


def test_a_renderer_that_fails_is_cannot_verify_not_a_flagged_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 1 is this script's word for a verdict that flagged the resolution, and
    the caller reports it as a claim about the merge. A renderer that never rendered
    the delta judged nothing, so it must not reach that number."""
    repo = repo_with_resolved_merge(tmp_path, RESOLUTION_FULLY_RETIRED)
    cfg = _config(tmp_path, repo, base_worktree=tmp_path / "empty-base")
    # The renderer ships with the resolver, so an absent one is not a state a
    # fixture tree can produce — the lookup itself is redirected instead.
    monkeypatch.setattr(
        type(cfg), "script", lambda self, name: str(tmp_path / "missing" / name)
    )
    with pytest.raises(SystemExit) as caught:
        sr.render_delta(cfg)
    assert caught.value.code == sr._EXIT_CANNOT_VERIFY
    assert "no reviewer read this resolution" in capsys.readouterr().err


def test_recording_the_spend_never_fails_the_review(tmp_path: Path) -> None:
    """A missing metric point costs less than a refused merge resolution, so a
    ledger writer that is not even there is not an error."""
    cfg = _config(tmp_path, tmp_path, base_worktree=tmp_path / "empty-base")
    sr._record_spend(cfg, tmp_path / "log.json")


def test_an_installer_that_fails_is_cannot_verify_not_a_flagged_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        sr._install_or_refuse(["bash", "-c", "exit 7"], cwd=tmp_path)
    assert caught.value.code == sr._EXIT_CANNOT_VERIFY
    assert "installer exited 7" in capsys.readouterr().err
    sr._install_or_refuse(["bash", "-c", "exit 0"], cwd=tmp_path)


def test_no_credential_at_all_is_cannot_verify(tmp_path: Path) -> None:
    cfg = _config(tmp_path, tmp_path, ladder=())
    with pytest.raises(SystemExit) as caught:
        sr.run_claude(cfg, tmp_path / "p.txt", tmp_path / "l.json")
    assert caught.value.code == sr._EXIT_CANNOT_VERIFY


def test_the_marker_scan_reads_the_shared_pattern(tmp_path: Path) -> None:
    """The pattern's `|{7}` branch matches diff3's `||||||| base` line, which
    prepare.sh writes: a scan without it reads that tree as fully resolved."""
    repo = repo_with_resolved_merge(tmp_path, RESOLUTION_FULLY_RETIRED)
    cfg = _config(tmp_path, repo)
    assert not sr._leaves_conflict_markers(cfg)
    (repo / "app.py").write_text("||||||| base\n", encoding="utf-8")
    git_in(repo, "add", "-A")
    assert sr._leaves_conflict_markers(cfg)


def test_the_marker_pattern_is_resolved_at_load_not_at_its_use_site() -> None:
    """A renamed `auto_resolve.conflict_marker_re` must stop the script before a
    review and a fix round have been paid for — and before the KeyError's exit 1 is
    read as _EXIT_FLAGGED, a verdict that run never reached. Reading the key inside
    `_leaves_conflict_markers` puts the failure after both."""
    assert sr._CONFLICT_MARKER_RE


# ------------------------------------------------------------------- one attempt


def _stub_claude(tmp_path: Path, body: str) -> Path:
    """A `claude` on PATH whose whole behavior is BODY, so one attempt's own arms
    are driven without the scripted ladder the subprocess suite uses."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "claude"
    stub.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    stub.chmod(0o755)
    return bin_dir


@pytest.mark.parametrize(
    ("credential", "authenticating", "cleared"),
    [
        ("sk-ant-oat-x", "oauth", "api"),
        ("sk-ant-api-x", "api", "oauth"),
    ],
)
def test_an_attempt_authenticates_through_the_var_its_shape_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential: str,
    authenticating: str,
    cleared: str,
) -> None:
    """The credential's shape decides the variable, and the OTHER one is unset — so
    a stale value from an earlier rung or the job's own env cannot leak in. The
    bound and the run's flags are read off the observed argv."""
    seen = tmp_path / "seen"
    bin_dir = _stub_claude(
        tmp_path,
        f'{{ printf "oauth=%s\\n" "${{CLAUDE_CODE_OAUTH_TOKEN-unset}}"\n'
        '  printf "api=%s\\n" "${ANTHROPIC_API_KEY-unset}"\n'
        '  printf "argv=%s\\n" "$*"\n'
        f'}} >"{seen}"\n'
        "printf '{\"is_error\": false}\\n'",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stale")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale")
    prompt = tmp_path / "p.txt"
    prompt.write_text("ask\n", encoding="utf-8")
    cfg = _config(tmp_path, tmp_path, timeout_seconds=77)
    assert sr.attempt_claude(cfg, credential, prompt, tmp_path / "log.json").verdict
    recorded = dict(
        line.split("=", 1) for line in seen.read_text(encoding="utf-8").splitlines()
    )
    assert recorded[authenticating] == credential
    assert recorded[cleared] == "unset"
    assert "--model claude-opus-5" in recorded["argv"]
    assert "--allowedTools Read,Edit,Write,Grep,Glob" in recorded["argv"]
    assert "--setting-sources user" in recorded["argv"]
    assert (tmp_path / "log.json.stderr").is_file(), (
        "the bound reports into its own log"
    )


def test_a_bounded_attempt_is_killed_at_the_configured_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A model run that never returns must not hold the resolve job past its own
    timeout, where a killed job pushes nothing."""
    bin_dir = _stub_claude(tmp_path, "sleep 30")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    prompt = tmp_path / "p.txt"
    prompt.write_text("ask\n", encoding="utf-8")
    cfg = _config(tmp_path, tmp_path, timeout_seconds=1)
    assert not sr.attempt_claude(
        cfg, "sk-ant-oat-x", prompt, tmp_path / "log.json"
    ).verdict
    assert "exited 124" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("exit 3", "exited 3"),
        ("printf 'not json'", "no parseable log"),
        ("printf '{\"is_error\": true}'", "reported is_error"),
        ("printf '[]'", "reported is_error"),
    ],
)
def test_an_attempt_with_no_verdict_says_which_way_it_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, message: str
) -> None:
    """A log that is not an object cannot answer `.is_error`, so it is a run with no
    verdict — never a clean read."""
    bin_dir = _stub_claude(tmp_path, body)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    prompt = tmp_path / "p.txt"
    prompt.write_text("ask\n", encoding="utf-8")
    cfg = _config(tmp_path, tmp_path, base_worktree=tmp_path / "empty-base")
    assert not sr.attempt_claude(
        cfg, "sk-ant-oat-x", prompt, tmp_path / "log.json"
    ).verdict


def test_a_metered_rung_says_the_run_bills_real_credits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bin_dir = _stub_claude(tmp_path, "printf '{\"is_error\": false}\\n'")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    prompt = tmp_path / "p.txt"
    prompt.write_text("ask\n", encoding="utf-8")
    cfg = _config(tmp_path, tmp_path)
    assert sr.attempt_claude(cfg, "sk-ant-api-x", prompt, tmp_path / "log.json").verdict
    assert "bills real credits" in capsys.readouterr().err


# ------------------------------------------------------------------ whole rounds


def _drive(
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rounds: str,
    max_rounds: int = 2,
    ladder: tuple[str, ...] = ("cred-1",),
    dead: tuple[str, ...] = (),
    permanent: tuple[str, ...] = (),
) -> None:
    """main() in-process against a real merge and the scripted fake `claude`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    claude = bin_dir / "claude"
    claude.write_text(FAKE_CLAUDE, encoding="utf-8")
    claude.chmod(0o755)
    (tmp_path / "counter").write_text("0", encoding="utf-8")
    # Pre-created so "no model ran" reads as an empty log rather than as a
    # missing file, which an assertion cannot tell from a broken fixture.
    (tmp_path / "tokens").write_text("", encoding="utf-8")
    (tmp_path / "vars").write_text("", encoding="utf-8")
    (tmp_path / "probes").write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("BASE_WORKTREE", str(REPO_ROOT))
    monkeypatch.setenv("SELF_REVIEW_DIR", str(tmp_path / "sr"))
    monkeypatch.setenv("MERGE_DELTA_MAX_ROUNDS", str(max_rounds))
    monkeypatch.setenv("SELF_REVIEW_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("SELF_REVIEW_TOKEN_LADDER", "\n".join(ladder))
    monkeypatch.setenv("ROUNDS", rounds)
    monkeypatch.setenv("ROUND_COUNTER", str(tmp_path / "counter"))
    monkeypatch.setenv("TOKEN_LOG", str(tmp_path / "tokens"))
    monkeypatch.setenv("VAR_LOG", str(tmp_path / "vars"))
    monkeypatch.setenv("PROBE_LOG", str(tmp_path / "probes"))
    monkeypatch.setenv("DEAD_TOKENS", ",".join(dead))
    monkeypatch.setenv("PERMANENT_TOKENS", ",".join(permanent))
    monkeypatch.setenv("HANG_TOKENS", "")
    monkeypatch.setenv("HANG_SECONDS", "30")
    monkeypatch.setenv("TARGET_FILE", str(repo / "app.py"))
    sr.main(["--repo", str(repo)])


def test_a_clean_read_leaves_the_merge_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = repo_with_resolved_merge(tmp_path, RESOLUTION_WITH_DELTA)
    before = git_in(repo, "rev-parse", "HEAD")
    _drive(tmp_path, repo, monkeypatch, rounds="clean")
    assert "reviews clean after 0 fix round(s)" in capsys.readouterr().out
    assert git_in(repo, "rev-parse", "HEAD") == before


def test_a_flagged_read_is_corrected_into_the_merge_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_resolved_merge(tmp_path, RESOLUTION_WITH_DELTA)
    before = git_in(repo, "rev-parse", "HEAD")
    _drive(tmp_path, repo, monkeypatch, rounds="flag,fix,clean")
    assert git_in(repo, "rev-parse", "HEAD") != before
    assert len(git_in(repo, "rev-list", "--parents", "-n1", "HEAD").split()) == 3


def test_a_resolution_still_flagged_at_the_cap_refuses_to_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = repo_with_resolved_merge(tmp_path, RESOLUTION_WITH_DELTA)
    with pytest.raises(SystemExit) as caught:
        _drive(tmp_path, repo, monkeypatch, rounds="flag", max_rounds=0)
    assert caught.value.code == sr._EXIT_FLAGGED
    assert "SMUGGLED" in capsys.readouterr().err


def test_a_fix_round_that_leaves_a_marker_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_resolved_merge(tmp_path, RESOLUTION_WITH_DELTA)
    before = git_in(repo, "rev-parse", "HEAD")
    with pytest.raises(SystemExit) as caught:
        _drive(tmp_path, repo, monkeypatch, rounds="flag,fix-marker:<<<<<<< HEAD,clean")
    assert caught.value.code == sr._EXIT_CANNOT_VERIFY
    assert git_in(repo, "rev-parse", "HEAD") == before


def test_a_reviewer_that_writes_no_verdict_is_cannot_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_resolved_merge(tmp_path, RESOLUTION_WITH_DELTA)
    with pytest.raises(SystemExit) as caught:
        _drive(tmp_path, repo, monkeypatch, rounds="silent")
    assert caught.value.code == sr._EXIT_CANNOT_VERIFY


def test_a_dead_rung_falls_through_to_the_next_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_resolved_merge(tmp_path, RESOLUTION_WITH_DELTA)
    _drive(
        tmp_path,
        repo,
        monkeypatch,
        rounds="clean",
        ladder=("cred-1", "cred-2", "cred-3"),
        dead=("cred-1",),
    )
    spent = (tmp_path / "tokens").read_text(encoding="utf-8").split()
    assert spent == ["cred-1", "cred-2"], "in order, and no further than needed"
    probed = (tmp_path / "probes").read_text(encoding="utf-8").split()
    assert probed == ["cred-2"], "every rung after the first is probed first"


def test_every_rung_dead_is_cannot_verify_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The floor the ladder never lowers: no verdict from any credential is still a
    refusal, never a bundle of an unreviewed resolution."""
    repo = repo_with_resolved_merge(tmp_path, RESOLUTION_WITH_DELTA)
    with pytest.raises(SystemExit) as caught:
        _drive(
            tmp_path,
            repo,
            monkeypatch,
            rounds="clean",
            ladder=("cred-1", "cred-2"),
            dead=("cred-1", "cred-2"),
        )
    assert caught.value.code == sr._EXIT_CANNOT_VERIFY
    assert (
        "no credential produced a verdict after 1 attempt(s)" in capsys.readouterr().err
    )
    # Rung 2 never reaches a round: its PROBE answered nothing, which is what
    # keeps a dead ladder off the wall clock the fix rounds need.
    assert (tmp_path / "tokens").read_text(encoding="utf-8").split() == ["cred-1"]
    assert (tmp_path / "probes").read_text(encoding="utf-8").split() == ["cred-2"]


def test_a_merge_git_resolved_by_itself_reaches_no_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A purely mechanical merge has no hand-authored delta, so there is nothing for
    a reviewer to judge and nothing to pay for."""
    repo = tmp_path / "mech"
    repo.mkdir()
    git_in(repo, "init", "-q", "-b", "main")
    git_in(repo, "config", "user.email", "t@t.t")
    git_in(repo, "config", "user.name", "t")
    (repo / "app.py").write_text("shared\n", encoding="utf-8")
    (repo / "other.py").write_text("shared\n", encoding="utf-8")
    git_in(repo, "add", "-A")
    git_in(repo, "commit", "-qm", "base")
    git_in(repo, "checkout", "-qb", "side")
    (repo / "app.py").write_text("side\n", encoding="utf-8")
    git_in(repo, "commit", "-qam", "side")
    git_in(repo, "checkout", "-q", "main")
    (repo / "other.py").write_text("main\n", encoding="utf-8")
    git_in(repo, "commit", "-qam", "main")
    git_in(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    _drive(tmp_path, repo, monkeypatch, rounds="crash")
    assert "nothing to review" in capsys.readouterr().out
    assert (tmp_path / "tokens").read_text(encoding="utf-8") == ""


def test_a_non_merge_head_is_nothing_to_self_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = repo_with_resolved_merge(tmp_path, RESOLUTION_FULLY_RETIRED)
    git_in(repo, "checkout", "-q", "HEAD^")
    _drive(tmp_path, repo, monkeypatch, rounds="crash")
    assert "not a merge commit" in capsys.readouterr().out


def test_the_cli_is_installed_from_the_trusted_resolver_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`claude` absent from PATH is the runner's cold start, and the installer is
    read from the resolver checkout rather than from the head under review."""
    repo = repo_with_resolved_merge(tmp_path, RESOLUTION_FULLY_RETIRED)
    base = tmp_path / "base"
    base.mkdir()
    marker = tmp_path / "installed"
    stub = tmp_path / "install-claude-cli.sh"
    stub.write_text(f"#!/usr/bin/env bash\ntouch {marker}\nexit 4\n", encoding="utf-8")
    # Only the installer is redirected: the renderer is read through the same
    # lookup and must stay the real one, or the run dies before the install branch.
    real_script = sr.SelfReviewConfig.script
    monkeypatch.setattr(
        sr.SelfReviewConfig,
        "script",
        lambda self, name: (
            str(stub) if name == "install-claude-cli.sh" else real_script(self, name)
        ),
    )
    # The system directories only: git and bash are there, `claude` is not, which
    # is what makes the install branch reachable.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("BASE_WORKTREE", str(base))
    monkeypatch.setenv("SELF_REVIEW_TOKEN_LADDER", "cred-1")
    monkeypatch.setenv("SELF_REVIEW_DIR", str(tmp_path / "sr"))
    with pytest.raises(SystemExit) as caught:
        sr.main(["--repo", str(repo)])
    assert caught.value.code == sr._EXIT_CANNOT_VERIFY
    assert marker.exists(), "the installer ran, from the resolver checkout"


# ------------------------------------------------ generated-output protection


def test_is_protected_generated_path_matches_a_directory_prefix() -> None:
    """`--owned` prints directory prefixes ending in `/` (`ownsPrefix`), and
    exact-equality alone misses a path under one — the fixer could then rewrite
    it with nothing to restore it."""
    owned = frozenset({"vendor/", "exact.txt"})
    assert sr._is_protected_generated_path("vendor/gen.txt", owned)
    assert sr._is_protected_generated_path("exact.txt", owned)
    assert not sr._is_protected_generated_path("other/gen.txt", owned)


def test_is_protected_generated_path_covers_a_builtin_lockfile_the_caller_owns_nothing_for() -> (
    None
):
    """The built-in registry (`_lockfiles.py`) is the fallback for a caller with
    NO declared rule at all — the empty `owned` set is its exact target, not a
    reason to skip protection."""
    assert sr._is_protected_generated_path("uv.lock", frozenset())
    assert not sr._is_protected_generated_path("README.md", frozenset())


def test_restore_generated_outputs_restores_a_builtin_lockfile_the_fixer_rewrote(
    tmp_path: Path,
) -> None:
    """The fixer holds Edit/Write with no per-path hook; this restore is what
    keeps its bytes out of a file only a generator may write. Regression: the
    original implementation returned early when the CALLER's own `--owned`
    table was empty, before ever checking whether a built-in lockfile was
    touched — which is the common case for a caller with no rule table."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, {"uv.lock": "before\n", "a.md": "a\n"}, "init")
    # Uncommitted, matching the real flow: the fixer edits the working tree and
    # this restore runs BEFORE the `git add -A; commit --amend` that would stage
    # it, so the comparison is against HEAD, not a second commit.
    (repo / "uv.lock").write_text("model-authored\n", encoding="utf-8")
    (repo / "a.md").write_text("also touched\n", encoding="utf-8")

    cfg = _config(tmp_path, repo)
    sr._restore_generated_outputs(cfg)

    assert (repo / "uv.lock").read_text(encoding="utf-8") == "before\n"
    assert (repo / "a.md").read_text(encoding="utf-8") == "also touched\n"


# ------------------------------------------------------- the credential ladder


@pytest.mark.parametrize(
    ("body", "permanent"),
    [
        ('{"api_error_status": 401}', True),
        ('{"api_error_status": 403}', True),
        ('{"api_error_status": "401"}', True),
        ('{"api_error_status": 429}', False),
        ('{"api_error_status": 500}', False),
        ('{"is_error": true}', False),
        ("[]", False),
        ("not json", False),
    ],
)
def test_only_a_rejected_credential_is_permanent(
    tmp_path: Path, body: str, permanent: bool
) -> None:
    """401 (revoked) and 403 (subscription access off) are decided outside this
    job, so a retry buys the answer already in hand. A 429 or a 500 is not: the
    same credential can answer the next call."""
    log = tmp_path / "log.json"
    log.write_text(body, encoding="utf-8")
    assert sr.is_permanently_dead(log) is permanent


def test_the_probe_bound_leaves_a_fix_round_inside_the_budget() -> None:
    """The arithmetic the bound exists for: seven rungs charged the round timeout
    cost more than the whole default budget, so the run reaches its deadline having
    attempted no correction."""
    cfg = sr.SelfReviewConfig(
        repo=Path(),
        base_worktree=Path(),
        review_dir=Path(),
        max_rounds=2,
        budget_seconds=1200,
        timeout_seconds=240,
        ladder=("a",),
    )
    assert cfg.probe_seconds == 30
    ladder_cost = cfg.timeout_seconds + 6 * cfg.probe_seconds
    assert cfg.budget_seconds - ladder_cost >= 2 * cfg.timeout_seconds
    assert 7 * cfg.timeout_seconds > cfg.budget_seconds


def test_a_walk_with_no_deadline_charges_the_whole_per_call_timeout() -> None:
    """The default: a caller that gave the ladder no budget gets today's bound."""
    assert sr.Ladder(credentials=("a",)).allowance(240) == 240


def test_an_attempt_is_bounded_by_what_is_left_of_the_shared_deadline(
    monkeypatch,
) -> None:
    """A probe says a rung REACHES the model; it does not say the real call returns.
    So the round's own timeout is not the bound — what the step has left is."""
    now = 1000.0
    monkeypatch.setattr(sr.time, "monotonic", lambda: now)
    ladder = sr.Ladder(credentials=("a",), deadline=now + 50)
    assert ladder.allowance(240) <= 50
    assert sr.Ladder(credentials=("a",), deadline=now - 1).allowance(240) == 0


def _hanging_run_cli(clock: list[float], seen: list[int], status: int = 124):
    """A `_run_cli` that always burns its whole allowance and answers nothing.

    STATUS picks which failure it answers with: 124 is the cap's own kill, which
    stops the walk, and any other status is a rung failure the walk continues past.
    """

    def run_cli(_cfg, _credential, _prompt, log, *, seconds, tools, cwd=None):
        seen.append(seconds)
        clock[0] += seconds
        log.write_text('{"is_error": true}', encoding="utf-8")
        return status

    return run_cli


def test_a_hanging_ladder_walk_cannot_outlast_the_step_budget(
    tmp_path, monkeypatch
) -> None:
    """The defect: every rung after the first reset the full 240 s timeout, so one
    walk of the 8-rung ladder could spend 2130 s of a 1200 s budget. The caller is
    inside `run_claude` and cannot read its own clock, so the walk had to stop
    itself."""
    clock = [1000.0]
    seen: list[int] = []
    monkeypatch.setattr(sr.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sr, "_run_cli", _hanging_run_cli(clock, seen, status=3))
    monkeypatch.setattr(sr, "_report_run_cause", lambda _log: None)
    cfg = _config(
        tmp_path,
        tmp_path,
        budget_seconds=1200,
        timeout_seconds=240,
        ladder=tuple(f"cred-{n}" for n in range(8)),
    )
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("review this\n", encoding="utf-8")
    ladder = sr.Ladder(credentials=cfg.ladder, deadline=1000.0 + cfg.budget_seconds)
    with pytest.raises(SystemExit) as exit_info:
        sr.run_claude(cfg, prompt, tmp_path / "review-0.json", ladder)
    assert exit_info.value.code == sr._EXIT_CANNOT_VERIFY
    assert clock[0] - 1000.0 <= cfg.budget_seconds
    assert sum(seen) == pytest.approx(clock[0] - 1000.0)
    assert 240 + 7 * (240 + 30) > cfg.budget_seconds, "what the bound prevents"


def test_a_call_the_cap_killed_stops_the_walk_at_one_attempt(
    tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_ladder.py` rule 5, at the walk that spends the money. The observed run
    charged six rungs the same cap for the same 124 and produced no verdict."""
    clock = [1000.0]
    seen: list[int] = []
    monkeypatch.setattr(sr.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sr, "_run_cli", _hanging_run_cli(clock, seen))
    monkeypatch.setattr(sr, "_report_run_cause", lambda _log: None)
    cfg = _config(
        tmp_path,
        tmp_path,
        budget_seconds=1200,
        timeout_seconds=300,
        ladder=tuple(f"cred-{n}" for n in range(8)),
    )
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("review this\n", encoding="utf-8")
    ladder = sr.Ladder(credentials=cfg.ladder, deadline=1000.0 + cfg.budget_seconds)
    with pytest.raises(SystemExit) as exit_info:
        sr.run_claude(cfg, prompt, tmp_path / "review-0.json", ladder)
    assert exit_info.value.code == sr._EXIT_CANNOT_VERIFY
    assert seen == [300], "one attempt, not a walk of every remaining rung"
    assert "reached its 300s cap" in capsys.readouterr().err


def test_the_rung_that_answered_is_tried_first_and_a_dead_one_not_at_all() -> None:
    """What the ladder learns inside ONE run: the walk is per model call, so a
    rung the review proved dead would otherwise be re-walked by the fix."""
    ladder = sr.Ladder(credentials=("a", "b", "c"))
    assert ladder.order() == [0, 1, 2]
    ladder.strike_off(0)
    ladder.preferred = 2
    assert ladder.order() == [2, 1]


def test_a_rung_revoked_after_it_answered_does_not_stay_at_the_head() -> None:
    """The mid-run revocation this bound exists for: the rung that answered the
    review is preferred, and a preferred rung leads every later walk AND skips the
    probe. Left there it is billed the full round timeout on a credential already
    known dead."""
    ladder = sr.Ladder(credentials=("a", "b", "c"), preferred=0)
    assert ladder.order() == [0, 1, 2]
    ladder.strike_off(0)
    assert ladder.preferred is None
    assert ladder.order() == [1, 2]
    # And the head is filtered against `dead` on the way out too, so a rung
    # struck off by any route cannot lead the next walk.
    revoked = sr.Ladder(credentials=("a", "b", "c"), preferred=1)
    revoked.dead.add(1)
    assert revoked.order() == [0, 2]
