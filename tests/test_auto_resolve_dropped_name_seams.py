"""Tests for the DROPPED-NAME SEAMS check (regression for agent-glovebox #4492:
a merge dropped --ref/MAIN_REF/on_main, and the caller merged clean and still
called them). Each case builds a real scratch repo and drives the merge
through actual git, then calls `main()` in-process against the real tree."""

import subprocess
from pathlib import Path

from tests._helpers import commit_files, git_env, git_out, init_test_repo
from tests._resolver_helpers import load_script

dropped_name_seams = load_script(".github/resolver/auto-resolve/dropped_name_seams.py")
main = dropped_name_seams.main

_METRICS_V0 = "def build_parser():\n    return None\n"
_METRICS_HEAD = "def build_parser():\n    return None  # head\n"


def _merge_keeping_head(
    repo: Path, head_branch: str, conflicting_path: str, head_content: str
) -> str:
    """Merge HEAD_BRANCH into the current branch, forcing CONFLICTING_PATH to
    HEAD_CONTENT — the declined resolution's own behavior, whether git flagged
    a real conflict there or merged it on its own. Returns the merge SHA."""
    result = subprocess.run(
        ["git", "merge", "--no-ff", head_branch, "-m", "merge"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    (repo / conflicting_path).write_text(head_content, encoding="utf-8")
    if result.returncode != 0:
        return commit_files(repo, {}, "merge: keep head side")
    git_out(repo, "add", "-A")
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit"], cwd=repo, env=git_env(), check=True
    )
    return git_out(repo, "rev-parse", "HEAD")


def _build_conflict(
    repo: Path,
    init_files: dict[str, str],
    base_metrics: str,
    base_extra: dict[str, str],
) -> tuple[str, str]:
    """A scratch repo where `metrics.py` conflicts between `main` (BASE_METRICS,
    plus BASE_EXTRA) and a `headbranch` that keeps METRICS_V0's shape, and the
    merge resolves by keeping the head side. Returns (base_sha, merge_sha)."""
    init_test_repo(repo)
    commit_files(repo, {"metrics.py": _METRICS_V0, **init_files}, "init")
    git_out(repo, "checkout", "-b", "headbranch")
    commit_files(repo, {"metrics.py": _METRICS_HEAD}, "head: unrelated tweak")
    git_out(repo, "checkout", "main")
    base_sha = commit_files(
        repo, {"metrics.py": base_metrics, **base_extra}, "base change"
    )
    merge_sha = _merge_keeping_head(repo, "headbranch", "metrics.py", _METRICS_HEAD)
    return base_sha, merge_sha


def test_flag_seam_names_the_caller(tmp_path, capsys):
    repo = tmp_path / "repo"
    base_metrics = (
        "import argparse\n\n\n"
        "def build_parser():\n"
        "    parser = argparse.ArgumentParser()\n"
        '    parser.add_argument("--ref", help="git ref")\n'
        "    return parser\n"
    )
    base_sha, merge_sha = _build_conflict(
        repo,
        init_files={
            "caller.sh": "#!/bin/sh\npython3 metrics.py\n",
            "untouched.py": "x = 1\n",
        },
        base_metrics=base_metrics,
        base_extra={"caller.sh": '#!/bin/sh\npython3 metrics.py --ref "$REF"\n'},
    )

    main(
        [
            "--merge",
            merge_sha,
            "--base",
            base_sha,
            "--repo",
            str(repo),
            "--",
            "metrics.py",
        ]
    )
    out = capsys.readouterr().out
    assert "--ref" in out
    assert "metrics.py" in out
    assert "caller.sh" in out


def test_identifier_seam_names_the_caller(tmp_path, capsys):
    repo = tmp_path / "repo"
    base_metrics = (
        'MAIN_REF = "main"\n\n\n'
        "def on_main(ref):\n"
        "    return ref == MAIN_REF\n\n\n"
        "def build_parser():\n"
        "    return None\n"
    )
    base_sha, merge_sha = _build_conflict(
        repo,
        init_files={"sibling.py": "import metrics\n\nprint(1)\n"},
        base_metrics=base_metrics,
        base_extra={"sibling.py": "import metrics\n\nprint(metrics.MAIN_REF)\n"},
    )

    main(
        [
            "--merge",
            merge_sha,
            "--base",
            base_sha,
            "--repo",
            str(repo),
            "--",
            "metrics.py",
        ]
    )
    out = capsys.readouterr().out
    assert "MAIN_REF" in out
    assert "sibling.py" in out
    assert "on_main" not in out


def test_dropped_and_unreferenced_name_is_silent(tmp_path, capsys):
    repo = tmp_path / "repo"
    base_metrics = 'OBSOLETE_FLAG = "gone"\n\n\ndef build_parser():\n    return None\n'
    base_sha, merge_sha = _build_conflict(
        repo,
        init_files={"other.py": "print(1)\n"},
        base_metrics=base_metrics,
        base_extra={},
    )

    main(
        [
            "--merge",
            merge_sha,
            "--base",
            base_sha,
            "--repo",
            str(repo),
            "--",
            "metrics.py",
        ]
    )
    assert capsys.readouterr().out == ""


def test_flag_boundary_excludes_partial_matches(tmp_path, capsys):
    repo = tmp_path / "repo"
    base_metrics = (
        'def build_parser():\n    parser.add_argument("--ref")\n    return None\n'
    )
    base_sha, merge_sha = _build_conflict(
        repo,
        init_files={
            "caller.sh": "#!/bin/sh\npython3 metrics.py --reference x --ref-name y\n"
        },
        base_metrics=base_metrics,
        base_extra={},
    )

    main(
        [
            "--merge",
            merge_sha,
            "--base",
            base_sha,
            "--repo",
            str(repo),
            "--",
            "metrics.py",
        ]
    )
    assert capsys.readouterr().out == ""


def test_flag_with_equals_form_is_reported(tmp_path, capsys):
    repo = tmp_path / "repo"
    base_metrics = (
        'def build_parser():\n    parser.add_argument("--ref")\n    return None\n'
    )
    base_sha, merge_sha = _build_conflict(
        repo,
        init_files={"caller.sh": "#!/bin/sh\npython3 metrics.py --ref=main\n"},
        base_metrics=base_metrics,
        base_extra={},
    )

    main(
        [
            "--merge",
            merge_sha,
            "--base",
            base_sha,
            "--repo",
            str(repo),
            "--",
            "metrics.py",
        ]
    )
    out = capsys.readouterr().out
    assert "--ref" in out
    assert "caller.sh" in out


def test_name_referenced_only_in_dropped_file_is_silent(tmp_path, capsys):
    repo = tmp_path / "repo"
    base_metrics = (
        'SELF_ONLY_NAME = "x"\n\n\n'
        "def uses_it():\n"
        "    return SELF_ONLY_NAME\n\n\n"
        "def build_parser():\n"
        "    return None\n"
    )
    base_sha, merge_sha = _build_conflict(
        repo, init_files={}, base_metrics=base_metrics, base_extra={}
    )

    main(
        [
            "--merge",
            merge_sha,
            "--base",
            base_sha,
            "--repo",
            str(repo),
            "--",
            "metrics.py",
        ]
    )
    assert capsys.readouterr().out == ""


def test_filtered_names_are_silent_even_when_referenced(tmp_path, capsys):
    repo = tmp_path / "repo"
    base_metrics = (
        "def _helper():\n    return 1\n\n\n"
        "x = 1\n"
        "logger = None\n\n\n"
        "def build_parser():\n    return None\n"
    )
    base_sha, merge_sha = _build_conflict(
        repo,
        init_files={
            "caller.py": "import metrics\n\nprint(metrics._helper, metrics.x, metrics.logger)\n"
        },
        base_metrics=base_metrics,
        base_extra={},
    )

    main(
        [
            "--merge",
            merge_sha,
            "--base",
            base_sha,
            "--repo",
            str(repo),
            "--",
            "metrics.py",
        ]
    )
    assert capsys.readouterr().out == ""


def test_unparseable_base_blob_warns_and_stays_silent(tmp_path, capsys):
    repo = tmp_path / "repo"
    base_sha, merge_sha = _build_conflict(
        repo,
        init_files={},
        base_metrics="def build_parser(:\n    return None\n",
        base_extra={},
    )

    main(
        [
            "--merge",
            merge_sha,
            "--base",
            base_sha,
            "--repo",
            str(repo),
            "--",
            "metrics.py",
        ]
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "::warning::" in captured.err
