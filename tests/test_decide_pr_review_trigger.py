"""The push-time re-review trigger reads the reviewer's latest verdict.

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
    */reviews) payload="PAGES" ;;
    */commits/*) payload="COMMIT" ;;
  esac
done
case "$payload" in
  PAGES) out="$(cat REVIEWS_JSON)" ;;
  COMMIT) out='"a commit with no opt-in tag"' ;;
  *) out="" ;;
esac
if [[ "$filtered" == true ]]; then
  jq -r "$filter" <<<"$out"
else
  printf '%s' "$out"
fi
"""


def _run(tmp_path: Path, reviews: list[dict]) -> dict[str, str]:
    """Run the trigger over one page of REVIEWS; return its GITHUB_OUTPUT."""
    pages = tmp_path / "reviews.json"
    pages.write_text(json.dumps([reviews]), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(GH_STUB.replace("REVIEWS_JSON", str(pages)), encoding="utf-8")
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
            "ACTION": "synchronize",
            "GITHUB_OUTPUT": str(out),
        },
    )
    assert res.returncode == 0, res.stderr
    return dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines()
    )


def _review(state: str, login: str = REVIEWER) -> dict:
    return {"state": state, "user": {"login": login}}


@pytest.mark.parametrize(
    ("state", "run"),
    [
        ("CHANGES_REQUESTED", "true"),
        ("COMMENTED", "true"),
        ("DISMISSED", "true"),
        ("APPROVED", "false"),
    ],
)
def test_a_push_re_reviews_exactly_the_verdicts_that_still_block(
    tmp_path: Path, state: str, run: str
):
    assert _run(tmp_path, [_review(state)])["run"] == run


def test_the_latest_verdict_wins_over_an_earlier_one(tmp_path: Path):
    reviews = [_review("CHANGES_REQUESTED"), _review("APPROVED")]
    assert _run(tmp_path, reviews)["run"] == "false"


def test_a_hold_left_by_someone_else_does_not_re_review(tmp_path: Path):
    assert _run(tmp_path, [_review("CHANGES_REQUESTED", "a-human")])["run"] == "false"


def test_no_reviews_at_all_does_not_re_review(tmp_path: Path):
    assert _run(tmp_path, [])["run"] == "false"
