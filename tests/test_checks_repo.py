"""Tests for the checks ported into .github/scripts/checks/: file-size,
grant-wildcards, gate-hooks-shimmed. Each check gets one input that must be
flagged and one that must pass, driven through the check's own pure functions
— mostly against synthetic content under tmp_path, plus one assertion against
this repo's own `.claude/settings.json` to prove the check accepts real input.
"""

import importlib.util
import json

import pytest

from tests._helpers import REPO_ROOT

_CHECKS = REPO_ROOT / ".github" / "scripts" / "checks"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _CHECKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


grant_wildcards = _load("grant-wildcards")
gate_hooks_shimmed = _load("gate-hooks-shimmed")
file_size = _load("file-size")


# ── grant-wildcards ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "spec",
    [
        # The motivating case: `git difftool` runs a command git config names.
        "git diff*",
        # Every character a command token may carry, one per case. A denylist of
        # `[A-Za-z0-9]` passed all of these while `Bash(foo_*)` auto-approves
        # `foo_bar` and `Bash(pre-*)` auto-approves `pre-commit`.
        "foo_*",
        "pre-*",
        "python3.*",
        "rg --*",
        "svc@*",
        "a,*",
        # A quote does not end a word: the shell joins adjacent fragments, so
        # `"git"tool` matches this grant and runs `gittool`.
        '"git"*',
        "'git'*",
        # A closing `)` or backtick joins what it closed to what follows, so
        # `$(printf git)tool` matches and runs `gittool`.
        "$(printf git)*",
        "`printf git`*",
        # Bash runs a file whose name containing `:` or `=`, so `foo:*` matches
        # the program `foo:tool` and `./foo=*` matches `./foo=tool`. Which word
        # is the executable does not change the answer: both are refused
        # wherever they sit, so no table of wrapper commands decides it.
        "foo:*",
        "pnpm test:*",
        "./foo=*",
        "git -c user.name=*",
        "sudo foo:*",
    ],
)
def test_grant_wildcards_flags_a_token_extending_star(spec: str) -> None:
    text = json.dumps({"permissions": {"allow": [f"Bash({spec})"]}})
    assert grant_wildcards.violations(text) == [1]


@pytest.mark.parametrize(
    "spec",
    [
        "git diff *",  # the two-form remedy's second grant
        "./scripts/*",  # a directory already fully named
        "*",  # opens the spec, so it extends nothing
        "echo ok; git diff *",  # a separator ends the previous command
    ],
)
def test_grant_wildcards_accepts_a_delimiter_star(spec: str) -> None:
    text = json.dumps({"permissions": {"allow": [f"Bash({spec})"]}})
    assert grant_wildcards.violations(text) == []


# ── gate-hooks-shimmed ───────────────────────────────────────────────────
def test_gate_hooks_shimmed_flags_raw_gate() -> None:
    settings = {
        "hooks": {
            "PreToolUse": [{"hooks": [{"type": "command", "command": "node gate.mjs"}]}]
        }
    }
    hits = gate_hooks_shimmed.unshimmed_gates(settings)
    assert len(hits) == 1 and "gate.mjs" in hits[0]


def test_gate_hooks_shimmed_accepts_launched_gate() -> None:
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "safe-launch.sh gate.mjs",
                        }
                    ]
                }
            ]
        }
    }
    assert gate_hooks_shimmed.unshimmed_gates(settings) == []


def test_real_settings_are_compliant() -> None:
    assert gate_hooks_shimmed.check_file(REPO_ROOT / ".claude" / "settings.json") == []


# ── file-size ────────────────────────────────────────────────────────────
def test_file_size_flags_new_violator_over_cap() -> None:
    policy = {"cap": 100, "baseline": {}}
    assert file_size.findings({"a.py": 150}, policy) == [
        "a.py: 150 lines exceeds the 100-line cap (new)."
    ]


def test_file_size_accepts_baselined_exact_match() -> None:
    policy = {"cap": 100, "baseline": {"a.py": 150}}
    assert file_size.findings({"a.py": 150}, policy) == []


def test_file_size_flags_grown_baseline_entry() -> None:
    policy = {"cap": 100, "baseline": {"a.py": 150}}
    findings = file_size.findings({"a.py": 200}, policy)
    assert len(findings) == 1 and "grew past its baseline" in findings[0]


def test_file_size_flags_stale_shrunk_baseline_entry() -> None:
    policy = {"cap": 100, "baseline": {"a.py": 150}}
    findings = file_size.findings({"a.py": 120}, policy)
    assert len(findings) == 1 and "stale entry" in findings[0]


def test_file_size_code_line_count_excludes_comments_and_blanks(tmp_path) -> None:
    src = tmp_path / "m.py"
    src.write_text("# a comment\n\nprint(1)\nprint(2)\n", encoding="utf-8")
    assert file_size._code_line_count(src, src.read_text(encoding="utf-8")) == 2
