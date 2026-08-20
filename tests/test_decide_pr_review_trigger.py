"""A pull request gets ONE whole-diff review; a later push is not re-read.

covers: .github/scripts/decide-pr-review-trigger.sh
covers: .github/resolver/lib/pr-reviews.bash

The `gh` stub here serves the shared library's GraphQL reviews read from a canned
node array, running the library's OWN `--jq` program through real jq — the
reviewer filter and the empty-body filter are the logic under test, so a stub that
answered a pre-filtered result would report the trigger working while testing
nothing. It also enforces the two argument rules real `gh api` enforces (`--slurp`
is rejected with `--jq`, and requires `--paginate`), so a call the real CLI would
refuse fails here too.
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
# GraphQL returns an app bot's login WITHOUT the REST `[bot]` suffix, which is the
# spelling the library's filter compares.
REVIEWER_BARE = "github-actions"

GH_STUB = """#!/usr/bin/env bash
set -uo pipefail
args=("$@")
slurp=false paginate=false filtered=false filter="" graphql=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --slurp) slurp=true; shift ;;
    --paginate) paginate=true; shift ;;
    --jq) filtered=true; filter="$2"; shift 2 ;;
    graphql) graphql=true; shift ;;
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
if [[ "$graphql" == true ]]; then
  payload="REVIEWS"
else
  for arg in "${args[@]}"; do
    case "$arg" in
      */commits/*) payload="COMMIT" ;;
    esac
  done
fi
case "$payload" in
  REVIEWS) out="$(cat FIXTURE_DIR/reviews.json)" ;;
  COMMIT) out="$(cat FIXTURE_DIR/commit.json)" ;;
  *) out="" ;;
esac
if [[ "$filtered" == true ]]; then
  jq -r "$filter" <<<"$out"
else
  printf '%s' "$out"
fi
"""


def _run(
    tmp_path: Path,
    reviews: list[dict],
    action: str = "synchronize",
    commit_subject: str = "a commit with no opt-in tag",
) -> dict[str, str]:
    """Run the trigger over one page of REVIEWS; return its GITHUB_OUTPUT."""
    (tmp_path / "reviews.json").write_text(
        json.dumps(
            {"data": {"repository": {"pullRequest": {"reviews": {"nodes": reviews}}}}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "commit.json").write_text(
        json.dumps({"commit": {"message": f"{commit_subject}\n\nbody line"}}),
        encoding="utf-8",
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


def _review(
    state: str,
    login: str = REVIEWER_BARE,
    *,
    body: str = "Automated review.",
    submitted_at: str = "2026-07-01T00:00:00Z",
) -> dict:
    """A review node, shaped per REVIEWS_QUERY in lib/pr-reviews.bash."""
    return {
        "author": {"login": login},
        "state": state,
        "body": body,
        "submittedAt": submitted_at,
        "fullDatabaseId": "4802416227",
        "commit": {"oid": HEAD_SHA},
    }


@pytest.mark.parametrize(
    "state", ["CHANGES_REQUESTED", "COMMENTED", "APPROVED", "DISMISSED"]
)
def test_a_push_does_not_re_review_a_pr_the_reviewer_already_read(
    tmp_path: Path, state: str
):
    # THE budget. Every state the reviewer can leave is a review it already spent,
    # so no push buys a second Opus pass. A dismissal is included: the review
    # carries no merge vote, so dismissing it moves nothing the gate reads.
    assert _run(tmp_path, [_review(state)])["run"] == "false"


def test_a_push_re_arms_the_read_the_reviewer_never_delivered(tmp_path: Path):
    # A cancelled job or an oversized diff leaves `opened`'s read owed, and no
    # other event re-arms it.
    assert _run(tmp_path, [])["run"] == "true"


def test_someone_elses_review_does_not_count_as_the_reviewer_read(tmp_path: Path):
    assert _run(tmp_path, [_review("APPROVED", "a-human")])["run"] == "true"


def test_an_empty_body_review_does_not_count_as_the_reviewer_read(tmp_path: Path):
    # GitHub synthesizes a body-less COMMENTED review around every standalone
    # review-comment POST, so counting one would spend the read on a thread reply.
    assert _run(tmp_path, [_review("COMMENTED", body="")])["run"] == "true"


def test_the_latest_review_decides_across_the_whole_page(tmp_path: Path):
    # The fold picks by submittedAt, not array order, so a page whose newest
    # review sits first still reads as reviewed.
    reviews = [
        _review("COMMENTED", submitted_at="2026-07-09T00:00:00Z"),
        _review("COMMENTED", submitted_at="2026-07-01T00:00:00Z"),
    ]
    assert _run(tmp_path, reviews)["run"] == "false"


def test_a_ready_for_review_after_the_read_does_not_review_again(tmp_path: Path):
    # A PR opened as a draft is reviewed on `opened`; marking it ready must not
    # buy a second read.
    out = _run(tmp_path, [_review("COMMENTED")], action="ready_for_review")
    assert out["run"] == "false"


def test_opened_always_reviews(tmp_path: Path):
    assert _run(tmp_path, [_review("COMMENTED")], action="opened")["run"] == "true"


TAGGED = "fix(gate): re-read the whole diff [opus-review]"


def test_the_opus_review_tag_on_a_pushed_head_buys_a_second_read(tmp_path: Path):
    # The tag is the only paid re-read left, so it is the one that must work.
    out = _run(tmp_path, [_review("APPROVED")], commit_subject=TAGGED)
    assert out["run"] == "true"


def test_the_opus_review_tag_does_not_fire_off_a_push(tmp_path: Path):
    # Head-scoped by design: one tagged commit buys one read. A ready_for_review
    # toggle carries no new commit, so honoring the tag there would buy one read
    # per toggle off a single head.
    out = _run(
        tmp_path,
        [_review("APPROVED")],
        action="ready_for_review",
        commit_subject=TAGGED,
    )
    assert out["run"] == "false"
