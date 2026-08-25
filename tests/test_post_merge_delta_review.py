"""The advisory merge-delta post step never calls an unreviewed head clean.

covers: .github/scripts/post-merge-delta-review.sh

The step runs whenever the RENDERER succeeded, including when the reviewer
step above it went red. So "the model wrote no file" and "there were no
deltas" are different states, and only the renderer can tell them apart.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
)
SCRIPT = REPO_ROOT / ".github/scripts/post-merge-delta-review.sh"

# Answers the four `gh api` shapes the script uses, and records every write to
# `calls.jsonl` so a test can assert what would reach the pull request.
GH_STUB = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

log = Path(os.environ["GH_CALL_LOG"])
argv = sys.argv[1:]
body = ""
for i, a in enumerate(argv):
    if a == "-F" and i + 1 < len(argv) and argv[i + 1].startswith("body=@"):
        body = Path(argv[i + 1][len("body=@"):]).read_text()
method = "GET"
if "-X" in argv:
    method = argv[argv.index("-X") + 1]
with log.open("a") as fh:
    fh.write(json.dumps({"method": method, "argv": argv, "body": body}) + "\\n")
# No comments exist on the pull request, so listings answer empty and the
# script takes its standalone-sticky path.
print("")
raise SystemExit(0)
"""


def _run(tmp_path: Path, *, had_deltas: str | None, review: str | None):
    """Run the post step and return (completed process, recorded gh calls)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "gh"
    stub.write_text(GH_STUB, encoding="utf-8")
    stub.chmod(0o755)

    pr_input = tmp_path / "pr-input"
    pr_input.mkdir()
    if review is not None:
        (pr_input / "merge-review.md").write_text(review, encoding="utf-8")

    log = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GH_CALL_LOG": str(log),
        "GH_TOKEN": "x",
        "GH_REPO": "o/r",
        "PR": "1",
        "PR_INPUT_DIR": str(pr_input),
    }
    env.pop("HAD_DELTAS", None)
    if had_deltas is not None:
        env["HAD_DELTAS"] = had_deltas

    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        cwd=REPO_ROOT,
        env=env,
        text=True,
    )
    calls = (
        [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        if log.exists()
        else []
    )
    return proc, calls


def test_deltas_with_no_review_is_reported_unreviewed_not_clean(tmp_path: Path):
    """The state a silent or crashed reviewer leaves behind."""
    proc, calls = _run(tmp_path, had_deltas="true", review="")
    assert proc.returncode == 0, proc.stderr

    posted = [c for c in calls if c["method"] == "POST"]
    assert posted, "an unreviewed head must not stay silent"
    assert "UNREVIEWED" in posted[0]["body"]
    assert "No merge-resolution deltas" not in posted[0]["body"]


def test_a_missing_review_file_is_also_unreviewed(tmp_path: Path):
    """The reviewer step died before writing anything at all."""
    proc, calls = _run(tmp_path, had_deltas="true", review=None)
    assert proc.returncode == 0, proc.stderr
    posted = [c for c in calls if c["method"] == "POST"]
    assert posted and "UNREVIEWED" in posted[0]["body"]


def test_no_deltas_says_so_and_posts_nothing_new(tmp_path: Path):
    """The genuinely clean state still reads as clean, and stays quiet."""
    proc, calls = _run(tmp_path, had_deltas="false", review=None)
    assert proc.returncode == 0, proc.stderr
    # Not a concern, and no sticky exists, so nothing is created.
    assert [c for c in calls if c["method"] == "POST"] == []


def test_an_unset_HAD_DELTAS_refuses_to_run(tmp_path: Path):
    """Inferring it from the review file is what this fix removed, so an
    unwired caller must fail loud rather than fall back to the old guess."""
    proc, _ = _run(tmp_path, had_deltas=None, review="")
    assert proc.returncode != 0
    assert "HAD_DELTAS" in proc.stderr


@pytest.mark.parametrize("had_deltas", ["true", "false"])
def test_the_workflow_passes_the_renderer_output(had_deltas: str):
    """The wiring itself: the post step reads the prepare step's output."""
    workflow = (REPO_ROOT / ".github/workflows/claude-review.yaml").read_text()
    assert "HAD_DELTAS: ${{ steps.prepare.outputs.has_deltas }}" in workflow
