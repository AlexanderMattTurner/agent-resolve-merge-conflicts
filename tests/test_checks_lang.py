"""Tests for the comment-block-length lint and the shared `_ratchet` baseline
logic under `.github/scripts/checks/`: at least one flagged input and one
passing input per case, run against synthetic tmp_path content."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

CHECKS_DIR = REPO_ROOT / ".github" / "scripts" / "checks"


def _load(name: str):
    src = CHECKS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKS_DIR / f"{name}.py"), *args],
        capture_output=True,
        text=True,
    )


# ── comment-block-length ─────────────────────────────────────────────────


def test_comment_block_length_flags_an_over_cap_note(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text(
        '"""doc."""\n\ndef foo():\n'
        + "".join(f"    # line {n} of prose that is not a list\n" for n in range(1, 7))
        + "    x = 1\n    return x\n",
        encoding="utf-8",
    )
    result = _run("comment-block-length", str(f))
    assert result.returncode == 1
    assert f"{f}: 1 violations exceeds the 0 cap (new)." in result.stderr
    # The block starts on line 4, and the refusal names it: a per-file total
    # alone leaves the author re-deriving which block to cut.
    assert f"{f}:4" in result.stderr


def test_comment_block_length_allows_a_short_note(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text(
        '"""doc."""\n\ndef foo():\n    # one short note\n    return 1\n',
        encoding="utf-8",
    )
    result = _run("comment-block-length", str(f))
    assert result.returncode == 0


def test_comment_block_length_header_cap_is_wider_than_note_cap(tmp_path: Path) -> None:
    # A block directly above an exported (non-underscore) def gets the 20-line
    # header cap, so 6 lines there must pass even though the same 6 lines mid-
    # body (asserted above) fail at the 5-line note cap.
    f = tmp_path / "m.py"
    f.write_text(
        '"""doc."""\n\n'
        + "".join(f"# line {n} of header prose\n" for n in range(1, 7))
        + "def foo():\n    return 1\n",
        encoding="utf-8",
    )
    result = _run("comment-block-length", str(f))
    assert result.returncode == 0


def test_violation_sites_names_only_the_files_the_ratchet_flagged() -> None:
    # `m.py` is a suffix of `sub/m.py`, so an unanchored match prints a file's
    # lines under another file's finding — pointing the author at a file the
    # ratchet never flagged.
    mod = _load("comment-block-length")
    sites = mod.violation_sites(
        {"m.py": [4], "sub/m.py": [9]},
        ["sub/m.py: 1 violations exceeds the 0 cap (new)."],
    )
    assert sites == ["sub/m.py:9"]


# ── _ratchet (shared grandfathered-baseline logic: file-size,
# comment-block-length, gate-hooks-shimmed all import this) ───────────────


def test_ratchet_allows_a_file_at_its_baselined_count() -> None:
    ratchet = _load("_ratchet")
    policy = {"cap": 0, "baseline": {"a.py": 3}}
    assert ratchet.findings({"a.py": 3}, policy, "violations") == []


def test_ratchet_flags_a_file_one_over_its_baselined_count() -> None:
    ratchet = _load("_ratchet")
    policy = {"cap": 0, "baseline": {"a.py": 3}}
    findings = ratchet.findings({"a.py": 4}, policy, "violations")
    assert len(findings) == 1 and "grew past its baseline of 3" in findings[0]


def test_ratchet_flags_an_unbaselined_file_with_a_violation() -> None:
    ratchet = _load("_ratchet")
    policy = {"cap": 0, "baseline": {}}
    findings = ratchet.findings({"a.py": 1}, policy, "violations")
    assert findings == ["a.py: 1 violations exceeds the 0 cap (new)."]


def test_ratchet_flags_a_stale_improved_baseline_entry() -> None:
    ratchet = _load("_ratchet")
    policy = {"cap": 0, "baseline": {"a.py": 3}}
    findings = ratchet.findings({"a.py": 0}, policy, "violations")
    assert len(findings) == 1 and "entry is stale" in findings[0]


def test_ratchet_flags_a_baseline_entry_for_a_deleted_file_in_a_complete_scan() -> None:
    ratchet = _load("_ratchet")
    policy = {"cap": 0, "baseline": {"gone.py": 2}}
    findings = ratchet.findings({}, policy, "violations", complete=True)
    assert len(findings) == 1 and "no matching file" in findings[0]


def test_ratchet_a_partial_scan_ignores_an_untouched_baseline_entry() -> None:
    """`complete=False` (a partial/argv-scoped scan) must not treat a
    baselined file simply absent from THIS run's file list as deleted."""
    ratchet = _load("_ratchet")
    policy = {"cap": 0, "baseline": {"untouched.py": 2}}
    findings = ratchet.findings({}, policy, "violations", complete=False)
    assert findings == []


def test_ratchet_load_policy_fails_loudly_on_a_missing_file(tmp_path: Path) -> None:
    ratchet = _load("_ratchet")
    with pytest.raises(ratchet.BaselineError):
        ratchet.load_policy(tmp_path / "missing.json")


def test_ratchet_load_policy_fails_loudly_on_unparseable_json(tmp_path: Path) -> None:
    ratchet = _load("_ratchet")
    bad = tmp_path / "baseline.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ratchet.BaselineError):
        ratchet.load_policy(bad)


# ── comment-block-length's own ratchet plumbing (scan_counts + findings) ──


def test_comment_block_length_ratchet_passes_a_file_at_its_baseline(
    tmp_path: Path,
) -> None:
    mod = _load("comment-block-length")
    f = tmp_path / "m.py"
    f.write_text(
        '"""doc."""\n\ndef foo():\n'
        + "".join(f"    # line {n} of prose that is not a list\n" for n in range(1, 7))
        + "    x = 1\n    return x\n",
        encoding="utf-8",
    )
    rel = str(f)
    policy = {"cap": 0, "baseline": {rel: 1}}
    counts = mod.scan_counts([rel])
    assert counts == {rel: 1}
    assert mod.findings(counts, policy, complete=False) == []


def test_comment_block_length_ratchet_flags_growth_past_baseline(
    tmp_path: Path,
) -> None:
    mod = _load("comment-block-length")
    f = tmp_path / "m.py"
    f.write_text(
        '"""doc."""\n\ndef foo():\n'
        + "".join(f"    # line {n} of prose that is not a list\n" for n in range(1, 7))
        + "    x = 1\n    return x\n",
        encoding="utf-8",
    )
    rel = str(f)
    policy = {"cap": 0, "baseline": {rel: 0}}
    counts = mod.scan_counts([rel])
    findings = mod.findings(counts, policy, complete=False)
    assert len(findings) == 1 and "grew past its baseline" in findings[0]
