"""A pull request gets ONE whole-diff review; a later push is not re-read.

covers: .github/scripts/decide-pr-review-trigger.sh

The `gh` stub here enforces the two argument rules real `gh api` enforces —
`--slurp` is rejected with `--jq`, and requires `--paginate`. A call the real
CLI would refuse must fail here too, or the test greens on a script that never
worked in CI.
"""

import json
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
SCRIPT = REPO_ROOT / ".github" / "scripts" / "decide-pr-review-trigger.sh"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
REVIEWER = "github-actions[bot]"

# Real `gh api` refuses these combinations at argument validation, before any
# request. A stub that answered them anyway would hide the defect this covers.
# `--paginate --jq` applies the filter PER PAGE, so the stub feeds each fixture
# to the script's own filter exactly as gh would.
GH_STUB = """#!/usr/bin/env bash
set -uo pipefail
args=("$@")
slurp=false paginate=false filtered=false filter=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --slurp) slurp=true; shift ;;
    --paginate) paginate=true; shift ;;
    --jq) filtered=true; filter="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if [[ "$slurp" == true && "$filtered" == true ]]; then
  echo "the \\`--slurp\\` option is not supported with \\`--jq\\` or \\`--template\\`" >&2
  exit 1
fi
if [[ "$slurp" == true && "$paginate" == false ]]; then
  echo "\\`--paginate\\` required when passing \\`--slurp\\`" >&2
  exit 1
fi
payload=""
for arg in "${args[@]}"; do
  case "$arg" in
    */reviews) payload="REVIEWS" ;;
    */timeline) payload="TIMELINE" ;;
    */commits/*) payload="COMMIT" ;;
  esac
done
case "$payload" in
  REVIEWS) out="$(cat FIXTURE_DIR/reviews.json)" ;;
  TIMELINE) out="$(cat FIXTURE_DIR/timeline.json)" ;;
  COMMIT) out='"a commit with no opt-in tag"' ;;
  *) out="" ;;
esac
if [[ "$filtered" == true ]]; then
  jq -r "$filter" <<<"$out"
else
  printf '%s' "$out"
fi
"""


HOLD_CLEARED_MARK = "[automated hold clearance]"


def _run(
    tmp_path: Path,
    reviews: list[dict],
    action: str = "synchronize",
    timeline: list[dict] | None = None,
) -> dict[str, str]:
    """Run the trigger over one page of REVIEWS; return its GITHUB_OUTPUT."""
    (tmp_path / "reviews.json").write_text(json.dumps(reviews), encoding="utf-8")
    (tmp_path / "timeline.json").write_text(
        json.dumps(timeline or []), encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(GH_STUB.replace("FIXTURE_DIR", str(tmp_path)), encoding="utf-8")
    gh.chmod(0o755)

    out = tmp_path / "github-output"
    out.touch()
    res = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
            "GH_TOKEN": "t",
            "REPO": "o/r",
            "PR": "8",
            "HEAD_SHA": HEAD_SHA,
            "ACTION": action,
            "GITHUB_OUTPUT": str(out),
        },
    )
    assert res.returncode == 0, res.stderr
    return dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines()
    )


def _review(state: str, login: str = REVIEWER) -> dict:
    return {"state": state, "user": {"login": login}, "body": "Automated review."}


def _dismissal(message: str) -> dict:
    return {
        "event": "review_dismissed",
        "dismissed_review": {"dismissal_message": message},
    }


@pytest.mark.parametrize("state", ["CHANGES_REQUESTED", "COMMENTED", "APPROVED"])
def test_a_push_does_not_re_review_a_pr_the_reviewer_already_read(
    tmp_path: Path, state: str
):
    # THE budget. Every verdict the reviewer can leave is a review it already
    # spent, so no push buys a second Opus pass — the hold clears when the
    # session resolves the threads (claude-reviewer-hold-clear.yaml).
    assert _run(tmp_path, [_review(state)])["run"] == "false"


def test_a_push_re_arms_the_read_the_reviewer_never_delivered(tmp_path: Path):
    # A cancelled job or an oversized diff leaves `opened`'s read owed, and no
    # other event re-arms it.
    assert _run(tmp_path, [])["run"] == "true"


def test_someone_elses_review_does_not_count_as_the_reviewer_read(tmp_path: Path):
    assert _run(tmp_path, [_review("APPROVED", "a-human")])["run"] == "true"


def test_a_ready_for_review_after_the_read_does_not_review_again(tmp_path: Path):
    # A PR opened as a draft is reviewed on `opened`; marking it ready must not
    # buy a second read.
    out = _run(tmp_path, [_review("COMMENTED")], action="ready_for_review")
    assert out["run"] == "false"


def test_opened_always_reviews(tmp_path: Path):
    assert _run(tmp_path, [_review("COMMENTED")], action="opened")["run"] == "true"


def test_a_human_dismissal_buys_the_pull_request_another_read(tmp_path: Path):
    # review-gate.sh drops a DISMISSED review, returning the PR to `pending`, so
    # only a new review clears it. A dismissal IS the request for that read.
    reviews = [_review("DISMISSED")]
    assert _run(tmp_path, reviews)["run"] == "true"


def test_the_sweepers_own_dismissal_buys_nothing(tmp_path: Path):
    # approve-if-reviewer-hold-clear.sh dismisses the reviewer's hold when GitHub
    # refuses it an approval. That dismissal SAYS the review stands, so it must
    # not re-arm the read its mark exists to preserve.
    out = _run(
        tmp_path,
        [_review("DISMISSED")],
        timeline=[_dismissal(f"cleared. {HOLD_CLEARED_MARK}")],
    )
    assert out["run"] == "false"
