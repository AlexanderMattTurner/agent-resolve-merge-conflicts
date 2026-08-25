"""The sticky comment is posted under the marker the job SEARCHES by.

covers: .github/scripts/remerge-diff-comment.sh

`report_render` renders with its own checkout's renderer; `report_comment`
derives the marker from the default branch. A PR that changes `MARKER` skews
the two, and a body posted under one marker while the search uses the other is
a fresh duplicate comment on every push.
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
)

_SCRIPT = REPO_ROOT / ".github" / "scripts" / "remerge-diff-comment.sh"
_TRUSTED = "<!-- trusted-marker -->"

_GH_STUB = """#!/usr/bin/env python3
import sys
from pathlib import Path

record = Path(__file__).with_name("recorded")
args = sys.argv[1:]
for arg in args:
    if arg.startswith("body=@"):
        record.write_text(Path(arg[len("body=@") :]).read_text(), encoding="utf-8")
        record.with_name("verb").write_text(" ".join(args), encoding="utf-8")
        raise SystemExit(0)
# The comment listing: no existing sticky comment.
raise SystemExit(0)
"""


def _resolver(tmp_path: Path) -> Path:
    """A resolver tree whose renderer declares a marker of our choosing."""
    resolver = tmp_path / "resolver"
    (resolver / "lib").mkdir(parents=True)
    (resolver / "lib" / "merge-delta-verdict.bash").write_bytes(
        (REPO_ROOT / ".github/resolver/lib/merge-delta-verdict.bash").read_bytes()
    )
    (resolver / "remerge-diff-report.py").write_text(
        f'MARKER = "{_TRUSTED}"\n', encoding="utf-8"
    )
    return resolver


def _run(tmp_path: Path, report: str) -> tuple[subprocess.CompletedProcess, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "gh"
    stub.write_text(_GH_STUB, encoding="utf-8")
    stub.chmod(0o755)

    report_file = tmp_path / "remerge-report.md"
    report_file.write_text(report, encoding="utf-8")

    done = subprocess.run(
        ["bash", str(_SCRIPT)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "GH_TOKEN": "x",
            "REPO": "owner/repo",
            "PR_NUMBER": "1",
            "REPORT_FILE": str(report_file),
            "RESOLVER_DIR": str(_resolver(tmp_path)),
        },
    )
    return done, bindir / "recorded"


def test_a_report_carrying_another_marker_posts_under_the_trusted_one(
    tmp_path: Path,
):
    """A head renderer that changed MARKER must not orphan the sticky comment."""
    done, recorded = _run(
        tmp_path, "<!-- head-marker -->\n## Hand-authored deltas\n\nbody line\n"
    )
    assert done.returncode == 0, done.stderr
    posted = recorded.read_text(encoding="utf-8")
    assert posted.splitlines()[0] == _TRUSTED
    assert "body line" in posted
    assert "<!-- head-marker -->" not in posted


def test_a_report_already_under_the_trusted_marker_is_posted_unchanged(
    tmp_path: Path,
):
    """The normalization must not rewrite the ordinary case."""
    body = f"{_TRUSTED}\n## Hand-authored deltas\n\nbody line\n"
    done, recorded = _run(tmp_path, body)
    assert done.returncode == 0, done.stderr
    assert recorded.read_text(encoding="utf-8") == body
