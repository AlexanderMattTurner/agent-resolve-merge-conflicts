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

import yaml

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
        body = Path(argv[i + 1][len("body=@"):]).read_text(encoding="utf-8")
method = "GET"
if "-X" in argv:
    method = argv[argv.index("-X") + 1]
with log.open("a", encoding="utf-8") as fh:
    fh.write(
        json.dumps(
            {
                "method": method,
                "argv": argv,
                "body": body,
                # The sticky lookup passes the marker through the environment
                # rather than splicing it into the jq filter, so this is where a
                # test reads which marker the script searched by.
                "marker": os.environ.get("GB_COMMENT_MARKER", ""),
            }
        )
        + "\\n"
    )
# No comments exist on the pull request, so listings answer empty and the
# script takes its standalone-sticky path.
print("")
raise SystemExit(0)
"""


def _run(
    tmp_path: Path,
    *,
    had_deltas: str | None,
    review: str | None,
    resolver_dir: Path | str | None = None,
):
    """Run the post step; return the process, the recorded gh calls, and its step outputs."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "gh"
    stub.write_text(GH_STUB, encoding="utf-8")
    stub.chmod(0o755)

    # The script pipes the model's review through sanitize-pr-input.mjs, whose
    # node_modules this suite does not install and whose behavior has its own
    # suite (sanitize-pr-input.test.mjs). Pass stdin through so these tests
    # exercise the branch logic rather than the sanitizer.
    node_stub = bindir / "node"
    node_stub.write_text("#!/usr/bin/env bash\nexec cat\n", encoding="utf-8")
    node_stub.chmod(0o755)

    pr_input = tmp_path / "pr-input"
    pr_input.mkdir()
    if review is not None:
        (pr_input / "merge-review.md").write_text(review, encoding="utf-8")

    log = tmp_path / "calls.jsonl"
    step_output = tmp_path / "step-output"
    step_output.touch()
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GH_CALL_LOG": str(log),
        "GITHUB_OUTPUT": str(step_output),
        "GH_TOKEN": "x",
        "GH_REPO": "o/r",
        "PR": "1",
        "PR_INPUT_DIR": str(pr_input),
        "RESOLVER_DIR": str(
            REPO_ROOT / ".github" / "resolver" if resolver_dir is None else resolver_dir
        ),
        # The sanitizer comes from the pinned tree, never the working directory.
        "RESOLVER_SCRIPTS": str(REPO_ROOT / ".github" / "scripts"),
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
        [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if log.exists()
        else []
    )
    outputs = dict(
        line.split("=", 1)
        for line in step_output.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return proc, calls, outputs


def test_deltas_with_no_review_is_reported_unreviewed_not_clean(tmp_path: Path):
    """The state a silent or crashed reviewer leaves behind."""
    proc, calls, _outputs = _run(tmp_path, had_deltas="true", review="")
    assert proc.returncode == 0, proc.stderr

    posted = [c for c in calls if c["method"] == "POST"]
    assert posted, "an unreviewed head must not stay silent"
    assert "UNREVIEWED" in posted[0]["body"]
    assert "No merge-resolution deltas" not in posted[0]["body"]


def test_a_missing_review_file_is_also_unreviewed(tmp_path: Path):
    """The reviewer step died before writing anything at all."""
    proc, calls, _outputs = _run(tmp_path, had_deltas="true", review=None)
    assert proc.returncode == 0, proc.stderr
    posted = [c for c in calls if c["method"] == "POST"]
    assert posted and "UNREVIEWED" in posted[0]["body"]


def test_no_deltas_says_so_and_posts_nothing_new(tmp_path: Path):
    """The genuinely clean state still reads as clean, and stays quiet."""
    proc, calls, _outputs = _run(tmp_path, had_deltas="false", review=None)
    assert proc.returncode == 0, proc.stderr
    # Not a concern, and no sticky exists, so nothing is created.
    assert [c for c in calls if c["method"] == "POST"] == []


def test_the_markers_come_from_the_pinned_resolver_not_a_literal(tmp_path: Path):
    """The writer matches the sticky the RENDERER wrote, and delimits its block
    with the delimiters the preserver carries — both read out of RESOLVER_DIR.

    Point RESOLVER_DIR at a copy whose renderer and lib carry different strings:
    a script restating either literal posts the repo's spelling and misses.
    """
    fake = tmp_path / "resolver"
    (fake / "lib").mkdir(parents=True)
    for rel in ("lib-ci-retry.sh", "lib-marker-comment.sh"):
        (fake / rel).write_bytes((REPO_ROOT / ".github/resolver" / rel).read_bytes())
    (fake / "remerge-diff-report.py").write_text(
        'MARKER = "<!-- other-renderer -->"\n', encoding="utf-8"
    )
    (fake / "lib" / "merge-delta-verdict.bash").write_text(
        (REPO_ROOT / ".github/resolver/lib/merge-delta-verdict.bash")
        .read_text(encoding="utf-8")
        .replace("<!-- merge-delta-review -->", "<!-- other-review -->")
        .replace("<!-- /merge-delta-review -->", "<!-- /other-review -->"),
        encoding="utf-8",
    )

    proc, calls, _outputs = _run(
        tmp_path, had_deltas="true", review="", resolver_dir=fake
    )
    assert proc.returncode == 0, proc.stderr

    searched = [c["marker"] for c in calls if c["method"] == "GET"]
    assert "<!-- other-renderer -->" in searched
    posted = [c for c in calls if c["method"] == "POST"]
    assert posted and posted[0]["body"].startswith("<!-- other-review -->")


def test_an_unset_RESOLVER_DIR_refuses_to_run(tmp_path: Path):
    """No fallback to a tree-relative resolver: a caller that wires no clone
    would otherwise match on some other sha's marker and post a duplicate."""
    proc, _calls, _outputs = _run(
        tmp_path, had_deltas="true", review="", resolver_dir=""
    )
    assert proc.returncode != 0
    assert "RESOLVER_DIR" in proc.stderr


def test_an_unset_HAD_DELTAS_refuses_to_run(tmp_path: Path):
    """Inferring it from the review file is what this fix removed, so an
    unwired caller must fail loud rather than fall back to the old guess."""
    proc, _calls, _outputs = _run(tmp_path, had_deltas=None, review="")
    assert proc.returncode != 0
    assert "HAD_DELTAS" in proc.stderr


def test_an_unrecognized_HAD_DELTAS_refuses_to_run(tmp_path: Path):
    """`:?` catches unset and empty, but every later test is `== "true"`.

    So `True` or `1` would take the else branch and publish "No merge-resolution
    deltas" with is_concern=false — the fail-open this change removes, reached
    through a typo in the workflow wiring instead of through the review file.
    """
    proc, calls, _outputs = _run(tmp_path, had_deltas="True", review="")
    assert proc.returncode != 0
    assert "HAD_DELTAS" in proc.stderr
    assert [c for c in calls if c["method"] in ("POST", "PATCH")] == []


def test_the_workflow_post_step_runs_even_when_an_earlier_step_failed():
    """Without `always()`, GitHub's implicit `success()` skips the post step
    whenever an earlier step reddened the job — and the reviewer step is
    earlier, so the one state this step exists to report would skip it. A
    skipped step's outcome is `skipped`, not `failure`, so the gate below would
    read the head as judged and publish green over it."""
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/claude-review.yaml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["merge_delta_review"]["steps"]
    post = next(s for s in steps if s.get("id") == "post_review")
    assert "always()" in post["if"]


def test_verdict_in_hand_is_false_when_the_reviewer_produced_nothing(tmp_path: Path):
    """The UNREVIEWED branch posts successfully and judges nothing.

    So the gate cannot key its MERGE_DELTA_VERDICT_IN_HAND exemption on this
    step's outcome: exiting 0 would skip the merge-delta term and publish green
    over a head no reviewer read.
    """
    proc, _calls, outputs = _run(tmp_path, had_deltas="true", review="")
    assert proc.returncode == 0, proc.stderr
    assert outputs["verdict_in_hand"] == "false"


def test_verdict_in_hand_is_true_when_the_reviewer_wrote_a_verdict(tmp_path: Path):
    proc, _calls, outputs = _run(tmp_path, had_deltas="true", review="No concerns.\n")
    assert proc.returncode == 0, proc.stderr
    assert outputs["verdict_in_hand"] == "true"


def test_review_clean_separates_a_clean_read_from_a_flagged_one(tmp_path: Path):
    """`verdict_in_hand` says a read HAPPENED, not what it found, so a caller
    gating on it alone cannot reject a flagged merge. These two states are
    identical to that output and must differ here."""
    clean_line = (
        (REPO_ROOT / ".github/resolver/lib/merge-delta-verdict.bash")
        .read_text(encoding="utf-8")
        .split('CLEAN_LINE="', 1)[1]
        .split('"', 1)[0]
    )
    for name in ("clean", "flagged"):
        (tmp_path / name).mkdir()
    clean, _c, clean_out = _run(
        tmp_path / "clean", had_deltas="true", review=clean_line + "\n"
    )
    flagged, _f, flagged_out = _run(
        tmp_path / "flagged",
        had_deltas="true",
        review="- the merge kept the base's assertion your fix invalidated\n",
    )

    assert clean.returncode == 0, clean.stderr
    assert flagged.returncode == 0, flagged.stderr
    # Identical on the older output — which is the gap.
    assert clean_out["verdict_in_hand"] == flagged_out["verdict_in_hand"] == "true"
    assert clean_out["review_clean"] == "true"
    assert flagged_out["review_clean"] == "false"


def test_review_clean_is_false_for_a_head_no_reviewer_read(tmp_path: Path):
    """Deltas present and no verdict written is the state a silent model leaves.
    A caller gating on this must fail CLOSED there."""
    proc, _calls, outputs = _run(tmp_path, had_deltas="true", review="")
    assert proc.returncode == 0, proc.stderr
    assert outputs["review_clean"] == "false"


def test_verdict_in_hand_is_true_when_there_were_no_deltas(tmp_path: Path):
    """Nothing to judge IS a verdict, so this state must not withhold it."""
    proc, _calls, outputs = _run(tmp_path, had_deltas="false", review=None)
    assert proc.returncode == 0, proc.stderr
    assert outputs["verdict_in_hand"] == "true"


def test_the_gate_exemption_requires_a_verdict_and_not_merely_a_non_failure():
    """`outcome != 'failure'` is true for a SKIPPED step and for a successful
    UNREVIEWED post alike, so the exemption must also read the step's own claim
    that a verdict exists."""
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/claude-review.yaml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["merge_delta_review"]["steps"]
    gate = next(s for s in steps if "review_findings_gate.py" in str(s.get("run", "")))
    expression = gate["env"]["MERGE_DELTA_VERDICT_IN_HAND"]
    assert "steps.post_review.outputs.verdict_in_hand == 'true'" in expression
    assert "steps.post_review.outcome != 'failure'" in expression


def test_the_workflow_passes_the_renderer_output():
    """The wiring itself: the post step's HAD_DELTAS reads the prepare step's
    output, so a silent reviewer cannot be mistaken for an absent one.

    Parsed rather than grepped: an exact-line match breaks on a reflow while the
    wiring is still correct, and passes on the string appearing in a comment.
    """
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/claude-review.yaml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["merge_delta_review"]["steps"]
    post = next(s for s in steps if s.get("id") == "post_review")
    assert "steps.prepare.outputs.has_deltas" in post["env"]["HAD_DELTAS"]
