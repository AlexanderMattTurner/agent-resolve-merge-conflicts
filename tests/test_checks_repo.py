"""Tests for the checks ported into .github/scripts/checks/: file-size,
gate-hooks-shimmed. Each check gets one input that must be flagged and one that
must pass, driven through the check's own pure functions — mostly against
synthetic content under tmp_path, plus one assertion against this repo's own
`.claude/settings.json` to prove the check accepts real input.
"""

import importlib.util

from tests._helpers import REPO_ROOT

_CHECKS = REPO_ROOT / ".github" / "scripts" / "checks"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _CHECKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate_hooks_shimmed = _load("gate-hooks-shimmed")
file_size = _load("file-size")


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
