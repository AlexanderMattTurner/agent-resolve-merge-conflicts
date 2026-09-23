"""The merge record every shard searches, written from a real mid-merge repository."""

import subprocess
from pathlib import Path

from tests._resolver_helpers import load_script

merge_context = load_script(".github/resolver/auto-resolve/_merge_context.py")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _mid_merge(tmp_path: Path) -> Path:
    """The #7124 shape: the PR retires `stream.sh` and adds its replacement, while the
    base branch fixes `stream.sh`. The PR also adds a symlink out of the tree."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "t"),
        ("user.email", "t@e"),
        ("commit.gpgsign", "false"),
    ):
        _git(work, "config", key, value)
    (work / "stream.sh").write_text("poll\n", encoding="utf-8")
    (work / "services.sh").write_text("start_stream\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "checkout", "-q", "-b", "pr")
    _git(work, "rm", "-q", "stream.sh")
    (work / "push.py").write_text("push\n", encoding="utf-8")
    (work / "leak").symlink_to("/etc/passwd")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "retire the poll loop")
    _git(work, "checkout", "-q", "main")
    (work / "stream.sh").write_text("poll\nfixed\n", encoding="utf-8")
    _git(work, "commit", "-q", "-am", "fix the poll loop")
    _git(work, "checkout", "-q", "pr")
    subprocess.run(["git", "-C", str(work), "merge", "main"], capture_output=True)
    return work


def test_the_record_holds_each_sides_version_and_intent(tmp_path, monkeypatch):
    work = _mid_merge(tmp_path)
    monkeypatch.chdir(work)
    monkeypatch.delenv("GH_REPO", raising=False)
    record = merge_context.write_context(tmp_path / "record", "7124")

    assert (record / "base-side/stream.sh").read_text(
        encoding="utf-8"
    ) == "poll\nfixed\n"
    assert (record / "merge-base/stream.sh").read_text(encoding="utf-8") == "poll\n"
    # Deleted on the PR side, so absent there: that absence is the deletion.
    assert not (record / "pr-side/stream.sh").exists()
    assert (record / "pr-side/push.py").read_text(encoding="utf-8") == "push\n"
    assert "retire the poll loop" in (record / "pr-side.log").read_text(
        encoding="utf-8"
    )
    assert "+fixed" in (record / "base-side.diff").read_text(encoding="utf-8")
    # A link in the copy would carry a Read past the directory the grant names.
    assert not (record / "pr-side/leak").exists()
    assert not (record / "pr-side/leak").is_symlink()


def test_a_rerun_replaces_the_previous_record(tmp_path, monkeypatch):
    work = _mid_merge(tmp_path)
    monkeypatch.chdir(work)
    monkeypatch.delenv("GH_REPO", raising=False)
    stale = tmp_path / "record" / "decided.md"
    stale.parent.mkdir()
    stale.write_text("- `x`: delete.\n", encoding="utf-8")
    merge_context.write_context(tmp_path / "record", "7124")
    assert not stale.exists()


def test_decided_text_names_each_answer_and_marks_a_non_answer(tmp_path):
    text = merge_context.write_decided(
        tmp_path,
        {
            "stream.sh": {"decision": "delete", "reasoning": "replaced by push.py"},
            "old.py": {"decision": "decline", "reasoning": ""},
            "gone.py": None,
        },
    )
    assert text == (
        "- `gone.py`: undecided.\n"
        "- `old.py`: undecided.\n"
        "- `stream.sh`: delete. replaced by push.py\n"
    )
    assert (tmp_path / "decided.md").read_text(encoding="utf-8") == text
    assert merge_context.write_decided(tmp_path / "none", {}) == ""


def test_outside_a_merge_there_is_no_record(tmp_path, monkeypatch, capsys):
    work = _mid_merge(tmp_path)
    _git(work, "merge", "--abort")
    monkeypatch.chdir(work)
    assert merge_context.write_context(tmp_path / "record", "7124") is None
    assert not (tmp_path / "record").exists()
    assert "without the merge record" in capsys.readouterr().err
