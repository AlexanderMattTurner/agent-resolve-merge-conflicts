"""Tests for .github/resolver/repolint/_linecheck.py — the machinery shared by
the line-oriented pre-commit lints under `.github/scripts/checks/`: the
read-each-path loop, the skip-on-unreadable, the `<path>:<lineno>: <message>`
print loop, the trailing `fix:` line, and the refusal each lint's `main()`
delegates to.
"""

from pathlib import Path

import pytest

from repolint._linecheck import report_line_checks, run_line_checks

_REMEDY = "delete the even lines."


def _even_lines(text: str) -> list[int]:
    """Toy detector: flag every line whose number is even (exercises the loop
    without coupling the loop test to any real lint's rules)."""
    return [n for n, _ in enumerate(text.splitlines(), 1) if n % 2 == 0]


def test_each_hit_prints_and_the_scan_reports_it_found_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "f.txt"
    f.write_text("a\nb\nc\nd\n", encoding="utf-8")  # lines 2 and 4 flagged
    assert report_line_checks([str(f)], _even_lines, "bad thing", remedy=_REMEDY)
    err = capsys.readouterr().err
    assert f"{f}:2: bad thing" in err
    assert f"{f}:4: bad thing" in err
    assert f"{f}:1:" not in err
    assert f"fix: {_REMEDY}" in err


def test_a_clean_scan_reports_nothing_and_prints_no_remedy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "f.txt"
    f.write_text("only one line\n", encoding="utf-8")  # no even line -> no hit
    assert not report_line_checks([str(f)], _even_lines, "msg", remedy=_REMEDY)
    assert capsys.readouterr().err == ""


def test_an_unreadable_path_is_skipped_rather_than_crashed_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A missing path raises OSError inside the loop and is skipped, not crashed on;
    # a real hit in another path still fires.
    bad = tmp_path / "hit.txt"
    bad.write_text("x\ny\n", encoding="utf-8")  # line 2 flagged
    missing = tmp_path / "nope.txt"  # never created -> OSError -> skipped
    assert report_line_checks(
        [str(missing), str(bad)], _even_lines, "msg", remedy=_REMEDY
    )
    assert f"{bad}:2: msg" in capsys.readouterr().err


def test_undecodable_bytes_are_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Non-UTF-8 bytes raise UnicodeDecodeError, which the loop swallows (the file
    # contributes nothing); the scan must not crash.
    f = tmp_path / "binary.txt"
    f.write_bytes(b"\xff\xfe\x00\x01")
    assert not report_line_checks([str(f)], _even_lines, "msg", remedy=_REMEDY)
    assert capsys.readouterr().err == ""


def test_a_single_detector_lint_refuses_on_a_hit(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("a\nb\n", encoding="utf-8")  # line 2 flagged
    with pytest.raises(SystemExit) as exc:
        run_line_checks([str(f)], _even_lines, "msg", remedy=_REMEDY)
    assert exc.value.code == 1


def test_an_empty_remedy_is_refused_rather_than_printed_bare(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("a\nb\n", encoding="utf-8")
    with pytest.raises(ValueError):
        report_line_checks([str(f)], _even_lines, "msg", remedy="   ")
