"""The review-findings merge gate: PENDING until the automated reviewer has posted
at least one review, RED while an unresolved reviewer-rooted thread still carries a
gating finding (read from the hidden `<!-- severity: … -->` marker on the thread's
ROOT comment, with the leading icon as a fallback), and RED or PENDING until the
merge-delta reviewer has judged the head. GREEN otherwise.

Two modes, one predicate:
  * REPORT_SHA set — the verdict is POSTed as a COMMIT STATUS on the context
    "Review findings resolved" on that sha; the run exits 0 whatever the verdict,
    and a failed POST is a hard red (a gate that cannot report leaves the required
    check hanging at "Expected").
  * REPORT_SHA unset — the exit status IS the report (merge_group mode).

Drives the REAL script (which calls the real pr-reviews.bash and
review-threads.bash through bash) with a fake `gh` on PATH that serves the two
GraphQL reads from canned node arrays by running the libraries' OWN `--jq` programs
through REAL jq — so the reviewer filter, the severity selection and the icon
fallback are exercised, never re-implemented — and records the commit-status POST's
fields for assertion.

covers: config/review-severities.json
covers: .github/scripts/review_findings_gate.py
covers: .github/scripts/consume-review-gate-recheck.sh
covers: .github/workflows/claude-review.yaml
covers: .github/workflows/review-findings-gate.yaml
covers: .github/workflows/review-findings-merge-gate.yaml
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "scripts" / "review_findings_gate.py"
GATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review-findings-gate.yaml"
# The merge-queue leg lives in its own merge_group-only workflow so no PR event can
# ever report the required context as a passing `skipped` instance.
MERGE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review-findings-merge-gate.yaml"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-review.yaml"


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(path: Path) -> dict:
    """A workflow's `on:` block. YAML 1.1 reads the bare key `on` as the boolean
    True, so the document carries it under whichever key the loader produced."""
    doc = _workflow(path)
    return doc.get("on", doc.get(True))


# The status context the gate posts must be byte-identical to the merge_gate job's
# `name:` (one required-check context, two reporting surfaces), so the expected
# name is READ from the workflow rather than restated here.
GATE_CONTEXT = _workflow(MERGE_WORKFLOW)["jobs"]["merge_gate"]["name"]
# The merge-delta reviewer's job name — the check run the gate's third term looks
# for on the head. Read from the workflow that defines the job, so a rename there
# moves these tests with it instead of leaving them asserting a dead name.
MERGE_DELTA_JOB_NAME = _workflow(REVIEW_WORKFLOW)["jobs"]["merge_delta_review"]["name"]

# The severity SSOT the gate builds its predicate from at runtime — the tests
# iterate its gating list member-by-member, so a severity added to the config
# without gate coverage fails here rather than in production.
SEVERITY_CONFIG = json.loads(
    (REPO_ROOT / "config" / "review-severities.json").read_text(encoding="utf-8")
)
GATING = SEVERITY_CONFIG["gating"]
GATING_ICONS = [SEVERITY_CONFIG["icons"][sev] for sev in GATING]
NON_GATING = [
    sev for sev in SEVERITY_CONFIG["icons"] if sev not in SEVERITY_CONFIG["gating"]
]

RUN_ID = "424242"
HEAD = "cafebabecafebabecafebabecafebabecafebabe"
# The BASE commit, where a `pull_request_target` job's check run would land if
# GitHub filed it there. Nothing the gate reads may find a run under this sha.
BASE = "d00df00dd00df00dd00df00dd00df00dd00df00d"


def _marker(severity: str) -> str:
    return f"<!-- severity: {severity} -->"


_FAKE_GH = r"""#!/usr/bin/env python3
# gh stub for the gate: serves the two GraphQL reads from canned node files,
# running the CALLER'S --jq through real jq (those filters are the logic under
# test), and records the commit-status POST's fields. Anything else is unhandled
# (exit 2), so a stray API call reds the run.
import json, os, re, shutil, subprocess, sys

JQ = shutil.which("jq")
if JQ is None:
    sys.stderr.write("fake gh: jq not found on PATH\n")
    sys.exit(3)

args = sys.argv[1:]
assert args and args[0] == "api", args
args = args[1:]

method, jq, path, fields = "GET", None, None, {}
i = 0
while i < len(args):
    a = args[i]
    if a == "--paginate":
        i += 1
    elif a in ("-X", "--method"):
        method, i = args[i + 1], i + 2
    elif a == "--jq":
        jq, i = args[i + 1], i + 2
    elif a in ("-F", "-f"):
        k, _, v = args[i + 1].partition("=")
        fields[k] = v
        i += 2
    elif not a.startswith("-"):
        path, i = a, i + 1
    else:
        i += 1


def emit(doc):
    r = subprocess.run(
        [JQ, "-r", jq], input=json.dumps(doc), text=True, capture_output=True
    )
    sys.stderr.write(r.stderr)
    sys.stdout.write(r.stdout)
    sys.exit(r.returncode)


if path == "graphql":
    posted_already = os.path.getsize(os.environ["STATUS_LOG"]) > 0
    if os.environ.get("FAIL_READS") == "1":
        sys.stderr.write("fake gh: HTTP 502 on the GraphQL read\n")
        sys.exit(1)
    query = fields.get("query", "")
    if "reviewThreads(" in query:
        # GH_THREADS_AFTER_POST is how a test moves the PR under a live run: once a
        # status has been recorded, the thread read serves the new state, which is
        # what a resolve landing mid-run looks like.
        later = os.environ.get("GH_THREADS_AFTER_POST")
        with open(
            later if later and posted_already else os.environ["GH_THREADS"],
            encoding="utf-8",
        ) as f:
            nodes = json.load(f)
        emit({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}})
    if "reviews(" in query:
        with open(os.environ["GH_REVIEWS"], encoding="utf-8") as f:
            nodes = json.load(f)
        emit({"data": {"repository": {"pullRequest": {"reviews": {"nodes": nodes}}}}})

check_runs = re.match(r"^repos/[^/]+/[^/]+/commits/(?P<sha>[^/?]+)/check-runs", path or "")
if check_runs and method == "GET":
    if os.environ.get("FAIL_CHECK_RUNS_READ") == "1":
        sys.stderr.write("fake gh: HTTP 502 on the check-runs read\n")
        sys.exit(1)
    # The query the gate asked for, so a test can assert the name is filtered
    # SERVER-side: a real head carries several pages of check runs.
    with open(os.environ["CHECK_RUNS_QUERY_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(fields) + "\n")
    # Filed BY SHA, as GitHub files them. Serving one list for whatever sha is asked
    # cannot tell a run reported on the head from one reported on the base, so it
    # would pass a gate that reads the wrong commit.
    with open(os.environ["GH_CHECK_RUNS"], encoding="utf-8") as f:
        by_sha = json.load(f)
    emit({"check_runs": by_sha.get(check_runs.group("sha"), [])})

pull_read = re.match(r"^repos/[^/]+/[^/]+/pulls/[0-9]+$", path or "")
if pull_read and method == "GET":
    with open(os.environ["GH_PULL"], encoding="utf-8") as f:
        emit(json.load(f))

status_read = re.match(r"^repos/[^/]+/[^/]+/commits/(?P<sha>[^/?]+)/status$", path or "")
if status_read and method == "GET":
    # The head's combined status, served from what has already been POSTed here, so
    # a run computing a verdict a previous run published really does find it.
    rows = []
    with open(os.environ["STATUS_LOG"], encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("sha") == status_read.group("sha"):
                rows.append(row)
    sys.stdout.write(json.dumps({"statuses": rows}))
    sys.exit(0)

status_post = re.match(r"^repos/[^/]+/[^/]+/statuses/(?P<sha>[^/]+)$", path or "")
if status_post and method == "POST":
    if os.environ.get("FAIL_STATUS_POST") == "1":
        sys.stderr.write("fake gh: HTTP 502 posting the status\n")
        sys.exit(1)
    with open(os.environ["STATUS_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(fields, sha=status_post.group("sha"))) + "\n")
    sys.exit(0)

sys.stderr.write("fake gh: unhandled %r\n" % (sys.argv,))
sys.exit(2)
"""


def review_node(
    login: str = "github-actions", body: str = "## Review\n\nfindings…"
) -> dict:
    """A completed review node, shaped per REVIEWS_QUERY in lib/pr-reviews.bash."""
    return {
        "author": {"login": login},
        "state": "COMMENTED",
        "body": body,
        "submittedAt": "2026-07-01T00:00:00Z",
        "fullDatabaseId": "4802416227",
        "commit": {"oid": HEAD},
    }


def thread_node(
    body: str,
    *,
    resolved: bool = False,
    login: str = "github-actions",
    path: str | None = "src/a.py",
    line: int | None = 3,
) -> dict:
    """A review-thread node, shaped per REVIEW_THREADS_QUERY in
    lib/review-threads.bash; `body`/`login` are the ROOT comment's."""
    return {
        "id": "PRRT_x",
        "isResolved": resolved,
        "isOutdated": False,
        "path": path,
        "line": line,
        "comments": {
            "nodes": [
                {
                    "fullDatabaseId": "1",
                    "author": {"login": login},
                    "body": body,
                    "pullRequestReview": {"fullDatabaseId": "4802416227"},
                }
            ]
        },
    }


def check_run(
    conclusion: str | None = "success",
    *,
    name: str = MERGE_DELTA_JOB_NAME,
    status: str = "completed",
) -> dict:
    """A check run as /commits/{sha}/check-runs returns it."""
    return {"name": name, "status": status, "conclusion": conclusion}


def gate_env(
    tmp_path: Path,
    *,
    reviews: list[dict],
    threads: list[dict],
    check_runs: list[dict] | None = None,
    check_runs_by_sha: dict[str, list[dict]] | None = None,
    draft: bool = False,
    author_type: str = "User",
) -> tuple[dict[str, str], Path]:
    """The environment a gate run needs — fake gh on the PATH front serving the
    canned nodes — and the file it appends each commit-status POST to."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    (tmp_path / "reviews.json").write_text(json.dumps(reviews), encoding="utf-8")
    (tmp_path / "threads.json").write_text(json.dumps(threads), encoding="utf-8")
    # Default: the merge-delta reviewer already judged THIS HEAD, so the third term
    # is satisfied for every case written to exercise (a) and (b). Keyed by sha, and
    # `check_runs_by_sha` is how a test files them somewhere else instead.
    (tmp_path / "check_runs.json").write_text(
        json.dumps(
            check_runs_by_sha
            if check_runs_by_sha is not None
            else {HEAD: [check_run()] if check_runs is None else check_runs}
        ),
        encoding="utf-8",
    )
    # The PR the gate reads to decide whether the merge-delta job declines it
    # outright. An ordinary human-authored, ready PR by default.
    (tmp_path / "pull.json").write_text(
        json.dumps({"draft": draft, "user": {"type": author_type}}), encoding="utf-8"
    )
    status_log = tmp_path / "statuses.ndjson"
    status_log.touch()
    query_log = tmp_path / "check-runs-queries.ndjson"
    query_log.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_TOKEN": "t",
        "GH_REPO": "o/r",
        "PR": "8",
        "GH_REVIEWS": str(tmp_path / "reviews.json"),
        "GH_THREADS": str(tmp_path / "threads.json"),
        "GH_CHECK_RUNS": str(tmp_path / "check_runs.json"),
        "GH_PULL": str(tmp_path / "pull.json"),
        "STATUS_LOG": str(status_log),
        "CHECK_RUNS_QUERY_LOG": str(query_log),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_RUN_ID": RUN_ID,
        "GITHUB_SERVER_URL": "https://github.com",
    }
    env.pop("REPORT_SHA", None)
    env.pop("MERGE_DELTA_VERDICT_IN_HAND", None)
    return env, status_log


def run_gate(env: dict[str, str], **overrides: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={**env, **overrides},
    )


def statuses(status_log: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in status_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def report(
    tmp_path: Path,
    *,
    reviews: list[dict],
    threads: list[dict],
    check_runs: list[dict] | None = None,
    check_runs_by_sha: dict[str, list[dict]] | None = None,
    draft: bool = False,
    author_type: str = "User",
    **overrides: str,
) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    """Run the gate in reporting mode on HEAD; return the run and the statuses."""
    env, log = gate_env(
        tmp_path,
        reviews=reviews,
        threads=threads,
        check_runs=check_runs,
        check_runs_by_sha=check_runs_by_sha,
        draft=draft,
        author_type=author_type,
    )
    done = run_gate(env, REPORT_SHA=HEAD, **overrides)
    return done, statuses(log)


# ── (a) the reviewer has to have reviewed ────────────────────────────────────


def test_pending_when_the_reviewer_never_reviewed(tmp_path: Path) -> None:
    done, posted = report(tmp_path, reviews=[], threads=[])
    assert done.returncode == 0, done.stderr
    assert [row["state"] for row in posted] == ["pending"]
    assert "has not reviewed this PR yet" in posted[0]["description"]


def test_a_non_reviewer_review_does_not_satisfy_the_first_leg(tmp_path: Path) -> None:
    done, posted = report(tmp_path, reviews=[review_node("a-human")], threads=[])
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "pending"


def test_an_empty_body_review_does_not_satisfy_the_first_leg(tmp_path: Path) -> None:
    # GitHub synthesizes a body-less COMMENTED review around every standalone
    # review-comment POST, so counting one would satisfy the leg vacuously.
    done, posted = report(tmp_path, reviews=[review_node(body="")], threads=[])
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "pending"


# ── (b) unresolved gating threads ────────────────────────────────────────────


@pytest.mark.parametrize("severity", GATING)
def test_red_on_an_unresolved_gating_severity_marker(
    tmp_path: Path, severity: str
) -> None:
    threads = [thread_node(f"a real problem\n\n{_marker(severity)}")]
    done, posted = report(tmp_path, reviews=[review_node()], threads=threads)
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "failure"
    assert "1 unresolved reviewer finding(s)" in posted[0]["description"]


@pytest.mark.parametrize("icon", GATING_ICONS)
def test_red_via_the_icon_fallback_for_pre_marker_threads(
    tmp_path: Path, icon: str
) -> None:
    threads = [thread_node(f"{icon} a real problem")]
    done, posted = report(tmp_path, reviews=[review_node()], threads=threads)
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "failure"


@pytest.mark.parametrize("severity", NON_GATING)
def test_green_when_only_a_non_gating_thread_is_unresolved(
    tmp_path: Path, severity: str
) -> None:
    threads = [thread_node(f"a style point\n\n{_marker(severity)}")]
    done, posted = report(tmp_path, reviews=[review_node()], threads=threads)
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "success"


def test_a_non_reviewer_thread_never_gates(tmp_path: Path) -> None:
    threads = [
        thread_node(f"drive-by\n\n{_marker(GATING[0])}", login="a-human"),
    ]
    done, posted = report(tmp_path, reviews=[review_node()], threads=threads)
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "success"


def test_green_once_every_gating_thread_is_resolved(tmp_path: Path) -> None:
    threads = [thread_node(f"fixed\n\n{_marker(GATING[0])}", resolved=True)]
    done, posted = report(tmp_path, reviews=[review_node()], threads=threads)
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "success"


def test_a_quoted_marker_in_prose_does_not_gate(tmp_path: Path) -> None:
    # The marker must own its whole LINE, so a finding that merely mentions one in
    # a sentence or a suggestion block cannot gate.
    body = f"the stamper writes `{_marker(GATING[0])}` on every finding"
    threads = [thread_node(body)]
    done, posted = report(tmp_path, reviews=[review_node()], threads=threads)
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "success"


def test_a_file_level_finding_names_the_path_without_a_line(tmp_path: Path) -> None:
    threads = [thread_node(f"PR-wide\n\n{_marker(GATING[0])}", line=None)]
    env, log = gate_env(tmp_path, reviews=[review_node()], threads=threads)
    done = run_gate(env, REPORT_SHA=HEAD)
    assert done.returncode == 0, done.stderr
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "src/a.py" in summary
    assert "src/a.py:None" not in summary
    assert statuses(log)[0]["state"] == "failure"


def test_the_red_summary_names_the_label_that_re_evaluates_it(tmp_path: Path) -> None:
    # Resolving a thread fires no workflow event, so the remedy is the only way a
    # session learns how to clear a stale red. It must name the REST route: `gh pr
    # edit --add-label` is GraphQL-backed and a web session answers it 403.
    label = json.loads(
        (REPO_ROOT / ".github" / "resolver" / "lib" / "shared-names.json").read_text(
            encoding="utf-8"
        )
    )["pr_labels"]["review_gate_recheck"]
    threads = [thread_node(f"a real problem\n\n{_marker(GATING[0])}")]
    env, _ = gate_env(tmp_path, reviews=[review_node()], threads=threads)
    run_gate(env, REPORT_SHA=HEAD)
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert f"issues/8/labels/{label}" in summary
    assert f"'labels[]={label}'" in summary
    assert "gh pr edit" in summary


# ── (c) the merge-delta reviewer's verdict for this head ─────────────────────


def test_pending_while_the_merge_delta_reviewer_has_not_run(tmp_path: Path) -> None:
    done, posted = report(tmp_path, reviews=[review_node()], threads=[], check_runs=[])
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "pending"
    assert MERGE_DELTA_JOB_NAME in posted[0]["description"]


def test_pending_while_the_merge_delta_reviewer_is_still_in_flight(
    tmp_path: Path,
) -> None:
    runs = [check_run(None, status="in_progress")]
    done, posted = report(
        tmp_path, reviews=[review_node()], threads=[], check_runs=runs
    )
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "pending"


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "failure"])
def test_red_when_the_merge_delta_run_ended_without_a_verdict(
    tmp_path: Path, conclusion: str
) -> None:
    runs = [check_run(conclusion)]
    done, posted = report(
        tmp_path, reviews=[review_node()], threads=[], check_runs=runs
    )
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "failure"


def test_green_once_the_merge_delta_run_published_a_verdict(tmp_path: Path) -> None:
    done, posted = report(
        tmp_path, reviews=[review_node()], threads=[], check_runs=[check_run()]
    )
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "success"


def test_a_label_events_skipped_instance_never_stands_in_for_a_verdict(
    tmp_path: Path,
) -> None:
    # claude-review.yaml also fires on `labeled`, where the merge-delta job's `if:`
    # declines the event and publishes a same-named `skipped` run on this head. The
    # gate's own remedy tells a session to bounce the review-gate-recheck label, so
    # counting that as judged would let the remedy green a head whose real run FAILED.
    runs = [check_run("failure"), check_run("skipped")]
    done, posted = report(
        tmp_path, reviews=[review_node()], threads=[], check_runs=runs
    )
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "failure"


def test_a_real_verdict_still_wins_over_a_label_events_skipped_sibling(
    tmp_path: Path,
) -> None:
    runs = [check_run("skipped"), check_run("success")]
    done, posted = report(
        tmp_path, reviews=[review_node()], threads=[], check_runs=runs
    )
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "success"


@pytest.mark.parametrize(
    ("draft", "author_type"), [(True, "User"), (False, "Bot")], ids=["draft", "bot"]
)
def test_the_term_is_dropped_for_a_pr_the_merge_delta_job_declines(
    tmp_path: Path, draft: bool, author_type: str
) -> None:
    # A draft cannot merge and a bot author is never Claude-reviewed, so that job's
    # eligibility guard declines every event and no run will ever judge the head.
    # Holding the term pending would strand the PR forever.
    done, posted = report(
        tmp_path,
        reviews=[review_node()],
        threads=[],
        check_runs=[],
        draft=draft,
        author_type=author_type,
    )
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "success"


def test_the_term_reads_the_check_runs_filed_on_the_head_not_the_base(
    tmp_path: Path,
) -> None:
    # A check run filed anywhere but the reported head answers nothing about it.
    done, posted = report(
        tmp_path,
        reviews=[review_node()],
        threads=[],
        check_runs_by_sha={BASE: [check_run()]},
    )
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "pending"
    assert MERGE_DELTA_JOB_NAME in posted[0]["description"]


def test_an_in_flight_re_run_outranks_a_sibling_that_ended_unjudged(
    tmp_path: Path,
) -> None:
    # A re-run of a job that died leaves both on the sha; the live instance is the
    # one that still answers, so this is pending rather than a red nobody can act on.
    runs = [check_run("cancelled"), check_run(None, status="queued")]
    done, posted = report(
        tmp_path, reviews=[review_node()], threads=[], check_runs=runs
    )
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "pending"


def test_another_jobs_check_run_never_satisfies_the_merge_delta_term(
    tmp_path: Path,
) -> None:
    # The gate re-compares the name, so a `check_name` filter the API ever stops
    # honouring cannot let an unrelated green stand in for the reviewer's verdict.
    runs = [check_run("success", name="Some other job")]
    done, posted = report(
        tmp_path, reviews=[review_node()], threads=[], check_runs=runs
    )
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "pending"


def test_the_check_run_read_filters_by_name_server_side(tmp_path: Path) -> None:
    env, _ = gate_env(tmp_path, reviews=[review_node()], threads=[])
    run_gate(env, REPORT_SHA=HEAD)
    queries = [
        json.loads(line)
        for line in Path(env["CHECK_RUNS_QUERY_LOG"])
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert queries, "the gate never read the head's check runs"
    assert queries[0]["check_name"] == MERGE_DELTA_JOB_NAME


def test_the_merge_delta_jobs_own_re_post_is_exempt(tmp_path: Path) -> None:
    # That job's re-post runs while its own check run is still in_progress, so
    # reading the term there would have the job publish red over its own verdict.
    done, posted = report(
        tmp_path,
        reviews=[review_node()],
        threads=[],
        check_runs=[],
        MERGE_DELTA_VERDICT_IN_HAND="true",
    )
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "success"


def test_a_failed_post_step_does_not_exempt_the_head(tmp_path: Path) -> None:
    done, posted = report(
        tmp_path,
        reviews=[review_node()],
        threads=[],
        check_runs=[],
        MERGE_DELTA_VERDICT_IN_HAND="false",
    )
    assert done.returncode == 0, done.stderr
    assert posted[0]["state"] == "pending"


# ── merge_group mode: the exit status IS the report ──────────────────────────


def test_merge_group_mode_exits_zero_on_green(tmp_path: Path) -> None:
    env, log = gate_env(tmp_path, reviews=[review_node()], threads=[], check_runs=[])
    done = run_gate(env)
    # No merge-delta verdict is required here: the queue's sha is ephemeral and no
    # reviewer ever ran on it, and the PR head carried the term before it queued.
    assert done.returncode == 0, done.stderr
    assert statuses(log) == [], "merge_group mode must post no status"


def test_merge_group_mode_exits_one_on_a_gating_thread(tmp_path: Path) -> None:
    threads = [thread_node(f"a real problem\n\n{_marker(GATING[0])}")]
    env, log = gate_env(tmp_path, reviews=[review_node()], threads=threads)
    done = run_gate(env)
    assert done.returncode == 1
    assert statuses(log) == []


# ── failing closed, and reporting mechanics ─────────────────────────────────


def test_a_failed_api_read_fails_closed_with_no_status_posted(tmp_path: Path) -> None:
    env, log = gate_env(tmp_path, reviews=[review_node()], threads=[])
    done = run_gate(env, REPORT_SHA=HEAD, FAIL_READS="1", RETRY_MAX="1")
    assert done.returncode == 1
    assert "failing closed" in done.stderr
    assert statuses(log) == []


def test_an_unreadable_check_run_list_reds_the_run_rather_than_the_head(
    tmp_path: Path,
) -> None:
    env, log = gate_env(tmp_path, reviews=[review_node()], threads=[])
    done = run_gate(env, REPORT_SHA=HEAD, FAIL_CHECK_RUNS_READ="1", RETRY_MAX="1")
    assert done.returncode == 1
    assert statuses(log) == []


def test_a_failed_status_post_fails_the_run_loudly(tmp_path: Path) -> None:
    # A gate that cannot report leaves the required check at "Expected", which
    # blocks the PR with nothing naming why.
    env, _ = gate_env(tmp_path, reviews=[review_node()], threads=[])
    done = run_gate(env, REPORT_SHA=HEAD, FAIL_STATUS_POST="1", RETRY_MAX="1")
    assert done.returncode == 1


def test_the_verdict_is_a_status_and_never_a_check_run(tmp_path: Path) -> None:
    done, posted = report(tmp_path, reviews=[review_node()], threads=[])
    assert done.returncode == 0, done.stderr
    assert [row["context"] for row in posted] == [GATE_CONTEXT]
    assert posted[0]["sha"] == HEAD


def test_the_status_links_the_run_that_holds_the_untruncated_reason(
    tmp_path: Path,
) -> None:
    done, posted = report(tmp_path, reviews=[review_node()], threads=[])
    assert done.returncode == 0, done.stderr
    assert posted[0]["target_url"].endswith(f"/actions/runs/{RUN_ID}")


def test_the_status_description_fits_the_api_cap(tmp_path: Path) -> None:
    threads = [
        thread_node(
            f"finding {n}\n\n{_marker(GATING[0])}", path=f"src/very/long/path/{n}.py"
        )
        for n in range(40)
    ]
    done, posted = report(tmp_path, reviews=[review_node()], threads=threads)
    assert done.returncode == 0, done.stderr
    assert len(posted[0]["description"]) <= 140


def test_the_status_description_carries_no_four_byte_character(tmp_path: Path) -> None:
    # A description holding a character above the BMP 422s the POST and hangs the
    # gate at "Expected"; every severity icon is one.
    done, posted = report(tmp_path, reviews=[review_node()], threads=[])
    assert done.returncode == 0, done.stderr
    assert all(ord(char) <= 0xFFFF for char in posted[0]["description"])


def test_a_settled_pr_publishes_the_verdict_exactly_once(tmp_path: Path) -> None:
    # A POST that changes nothing still writes a status and wakes every subscriber.
    env, log = gate_env(tmp_path, reviews=[review_node()], threads=[])
    assert run_gate(env, REPORT_SHA=HEAD).returncode == 0
    assert run_gate(env, REPORT_SHA=HEAD).returncode == 0
    assert len(statuses(log)) == 1


def test_a_changed_verdict_still_posts_over_the_published_one(tmp_path: Path) -> None:
    env, log = gate_env(tmp_path, reviews=[review_node()], threads=[])
    assert run_gate(env, REPORT_SHA=HEAD).returncode == 0
    (tmp_path / "threads.json").write_text(
        json.dumps([thread_node(f"new\n\n{_marker(GATING[0])}")]), encoding="utf-8"
    )
    assert run_gate(env, REPORT_SHA=HEAD).returncode == 0
    assert [row["state"] for row in statuses(log)] == ["success", "failure"]


def test_a_resolve_landing_mid_run_is_republished_by_the_run_it_raced(
    tmp_path: Path,
) -> None:
    # The reconcile loop's whole job: a run that published a stale verdict corrects
    # it itself, so a wrong verdict is never a resting state.
    threads = [thread_node(f"a real problem\n\n{_marker(GATING[0])}")]
    env, log = gate_env(tmp_path, reviews=[review_node()], threads=threads)
    (tmp_path / "threads-after.json").write_text(json.dumps([]), encoding="utf-8")
    done = run_gate(
        env,
        REPORT_SHA=HEAD,
        GH_THREADS_AFTER_POST=str(tmp_path / "threads-after.json"),
    )
    assert done.returncode == 0, done.stderr
    assert [row["state"] for row in statuses(log)] == ["failure", "success"]


# ── the workflow wiring the predicate depends on ─────────────────────────────


def test_the_merge_gate_workflow_triggers_only_on_merge_group() -> None:
    # A required check's job in a workflow that also fires on PR events reports
    # `skipped` on every PR-event run, and GitHub counts `skipped` as satisfying a
    # required check — a stray label would overwrite a red gate.
    assert list(_triggers(MERGE_WORKFLOW)) == ["merge_group"]


def test_the_merge_gate_job_carries_the_required_check_annotation() -> None:
    # sync-required-checks reads the annotation to rewrite the ruleset, so without
    # it the context is required nowhere and the gate blocks nothing.
    text = MERGE_WORKFLOW.read_text(encoding="utf-8")
    assert f"name: {GATE_CONTEXT} # required-check: true" in text


def test_the_merge_gate_step_passes_the_merge_group_shas() -> None:
    # The queue ref names only the LAST batch member, so the batch script reads the
    # roster from the base_sha..head_sha merge commits instead.
    step = _workflow(MERGE_WORKFLOW)["jobs"]["merge_gate"]["steps"][-1]
    assert "review-findings-merge-gate-batch.sh" in step["run"]
    assert set(step["env"]) >= {"MG_REF", "MG_BASE_SHA", "MG_HEAD_SHA"}


def test_the_gate_workflow_declares_only_triggers_actions_can_run() -> None:
    # `pull_request_review_thread` is a webhook event, not an Actions trigger:
    # naming it makes the file invalid, and GitHub then fails a zero-job run on
    # every push in the repo while nothing reports the breakage.
    assert set(_triggers(GATE_WORKFLOW)) == {
        "pull_request_target",
        "pull_request_review",
        "pull_request_review_comment",
    }


@pytest.mark.parametrize(
    "trigger,action",
    [
        ("pull_request_target", "synchronize"),
        ("pull_request_target", "labeled"),
        ("pull_request_review", "submitted"),
        ("pull_request_review_comment", "created"),
    ],
)
def test_the_gate_keeps_the_activity_types_a_stuck_pr_depends_on(
    trigger: str, action: str
) -> None:
    assert action in _triggers(GATE_WORKFLOW)[trigger]["types"]


def test_the_evaluate_job_runs_unconditionally_so_every_entrant_posts() -> None:
    # A conditionally skipped run could occupy the concurrency slot and post
    # nothing, leaving the head with a stale status or none at all.
    job = _workflow(GATE_WORKFLOW)["jobs"]["evaluate"]
    assert "if" not in job


def test_the_label_removal_precedes_the_evaluation_and_is_unconditional() -> None:
    # `labeled` fires only on a transition, so a label left sitting on the PR makes
    # the next re-add a no-op and the PR waits on a verdict nobody posts.
    steps = _workflow(GATE_WORKFLOW)["jobs"]["evaluate"]["steps"]
    runs = [step.get("run", "") for step in steps]
    consume = next(i for i, r in enumerate(runs) if "consume-review-gate-recheck" in r)
    evaluate = next(i for i, r in enumerate(runs) if "review_findings_gate.py" in r)
    assert consume < evaluate
    assert "if" not in steps[consume]


def test_the_evaluate_job_holds_every_scope_the_verdict_needs() -> None:
    perms = _workflow(GATE_WORKFLOW)["jobs"]["evaluate"]["permissions"]
    assert perms["statuses"] == "write"
    assert perms["checks"] == "read"
    # A label write on a PULL REQUEST routes through Pull requests, not Issues, so
    # `pull-requests: read` would 403 the DELETE and the label would stick forever.
    assert perms["pull-requests"] == "write"
    assert perms["issues"] == "write"


@pytest.mark.parametrize("job", ["review", "note-skipped-review", "merge_delta_review"])
def test_every_predicate_changing_job_reposts_the_gate_on_the_head(job: str) -> None:
    # Each of these posts a review or a finding thread with the workflow
    # GITHUB_TOKEN, whose events fire no workflows — so review-findings-gate.yaml
    # never hears about them and the head keeps a stale verdict.
    spec = _workflow(REVIEW_WORKFLOW)["jobs"][job]
    assert spec["permissions"]["statuses"] == "write"
    assert spec["permissions"]["checks"] == "read"
    repost = [
        step
        for step in spec["steps"]
        if "review_findings_gate.py" in step.get("run", "")
    ]
    assert len(repost) == 1, f"{job} does not re-post the gate verdict"
    assert repost[0]["env"]["REPORT_SHA"]


@pytest.mark.parametrize("job", ["review", "merge_delta_review"])
def test_the_repost_survives_a_failing_step_above_it(job: str) -> None:
    # An implicit success() skips the re-post whenever an earlier step reddened the
    # job — exactly when the head's verdict is stale.
    spec = _workflow(REVIEW_WORKFLOW)["jobs"][job]
    repost = next(
        step
        for step in spec["steps"]
        if "review_findings_gate.py" in step.get("run", "")
    )
    assert repost["if"].startswith("always()")


def test_only_the_merge_delta_jobs_repost_claims_the_verdict_exemption() -> None:
    # The exemption skips term (c). Any other job claiming it would publish green
    # on a head whose merge deltas nobody read.
    jobs = _workflow(REVIEW_WORKFLOW)["jobs"]
    claimants = {
        name
        for name, spec in jobs.items()
        for step in spec.get("steps", [])
        if "MERGE_DELTA_VERDICT_IN_HAND" in (step.get("env") or {})
    }
    assert claimants == {"merge_delta_review"}


# ── consume-review-gate-recheck.sh: the label the gate's own remedy re-adds ───

CONSUME = REPO_ROOT / ".github" / "scripts" / "consume-review-gate-recheck.sh"
RECHECK_LABEL = json.loads(
    (REPO_ROOT / ".github" / "resolver" / "lib" / "shared-names.json").read_text(
        encoding="utf-8"
    )
)["pr_labels"]["review_gate_recheck"]

_CONSUME_FAKE_GH = r"""#!/usr/bin/env python3
# gh stub for consume-review-gate-recheck.sh: answers the label READ from
# GH_LABELS (or fails when READ_STATUS says so), records every DELETE it is
# asked for, and answers it with DELETE_STATUS/DELETE_BODY.
import os, sys

args = sys.argv[1:]
if args[:2] == ["pr", "view"]:
    status = int(os.environ.get("READ_STATUS", "0"))
    if status:
        sys.stderr.write("fake gh: HTTP %d reading the labels\n" % status)
        sys.exit(1)
    sys.stdout.write(os.environ["GH_LABELS"])
    sys.exit(0)

if args[0] == "api" and "-X" in args and args[args.index("-X") + 1] == "DELETE":
    with open(os.environ["DELETE_LOG"], "a", encoding="utf-8") as f:
        f.write(args[-1] + "\n")
    status = int(os.environ.get("DELETE_STATUS", "0"))
    if status:
        sys.stderr.write(os.environ["DELETE_BODY"] + "\n")
        sys.exit(1)
    sys.exit(0)

sys.stderr.write("fake gh: unhandled %r\n" % (sys.argv,))
sys.exit(2)
"""


def run_consume(
    tmp_path: Path,
    *,
    labels: str,
    delete_status: int = 0,
    delete_body: str = "",
    read_status: int = 0,
    github_env: bool = True,
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    """Drive the REAL script with a fake `gh`; return the run, the DELETE paths it
    asked for, and whatever it recorded in GITHUB_ENV."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(_CONSUME_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    delete_log = tmp_path / "deletes.txt"
    delete_log.touch()
    env_file = tmp_path / "github_env"
    env_file.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_REPO": "o/r",
        "PR": "8",
        "GH_LABELS": labels,
        "DELETE_LOG": str(delete_log),
        "DELETE_STATUS": str(delete_status),
        "DELETE_BODY": delete_body,
        "READ_STATUS": str(read_status),
        # lib-ci-retry.sh sleeps 2s, doubling, between its 5 attempts, so a failing
        # case would spend 30 seconds asleep. Zero keeps every attempt and drops the
        # wait, so the retry path is still exercised.
        "RETRY_BASE_DELAY": "0",
    }
    if github_env:
        env["GITHUB_ENV"] = str(env_file)
    else:
        env.pop("GITHUB_ENV", None)
    done = subprocess.run(
        ["bash", str(CONSUME)], capture_output=True, text=True, check=False, env=env
    )
    deletes = [
        line for line in delete_log.read_text(encoding="utf-8").splitlines() if line
    ]
    return done, deletes, env_file.read_text(encoding="utf-8")


def test_the_recheck_label_is_deleted_when_the_pr_carries_it(tmp_path: Path) -> None:
    done, deletes, recorded = run_consume(
        tmp_path, labels=f"some-other-label\n{RECHECK_LABEL}\n"
    )
    assert done.returncode == 0, done.stderr
    assert deletes == [f"repos/o/r/issues/8/labels/{RECHECK_LABEL}"]
    assert recorded == ""


def test_no_delete_is_attempted_when_the_label_is_absent(tmp_path: Path) -> None:
    # The common run: retrying a DELETE that was always going to 404 would spend the
    # whole backoff on a no-op.
    done, deletes, recorded = run_consume(tmp_path, labels="merge-conflict\nclaude\n")
    assert done.returncode == 0, done.stderr
    assert deletes == []
    assert recorded == ""


def test_a_label_whose_name_merely_contains_the_recheck_name_is_not_a_match(
    tmp_path: Path,
) -> None:
    # The match is newline-delimited over the whole set, so a longer label carrying
    # the recheck name as a substring must not draw a DELETE of the real one.
    done, deletes, _ = run_consume(tmp_path, labels=f"{RECHECK_LABEL}-pending\n")
    assert done.returncode == 0, done.stderr
    assert deletes == []


def test_a_404_mid_run_is_tolerated_rather_than_recorded(tmp_path: Path) -> None:
    # Another writer removed the label between the read and this DELETE.
    done, deletes, recorded = run_consume(
        tmp_path,
        labels=f"{RECHECK_LABEL}\n",
        delete_status=1,
        delete_body="gh: Label does not exist (HTTP 404)",
    )
    assert done.returncode == 0, done.stderr
    assert deletes, "the script never tried the DELETE"
    assert recorded == ""


def test_a_real_delete_failure_is_recorded_and_never_raised(tmp_path: Path) -> None:
    # Exiting non-zero would skip the verdict post that follows and leave the head's
    # required check missing, which blocks the PR harder than a stuck label does.
    done, _, recorded = run_consume(
        tmp_path,
        labels=f"{RECHECK_LABEL}\n",
        delete_status=1,
        delete_body="gh: Server Error (HTTP 500)",
    )
    assert done.returncode == 0, done.stderr
    assert recorded.strip() == "LABEL_REMOVAL_FAILED=1"
    assert f"::error::{RECHECK_LABEL} label removal failed" in done.stdout


def test_a_real_delete_failure_off_a_runner_still_exits_zero(tmp_path: Path) -> None:
    # `set -u` plus an unset GITHUB_ENV would abort the script on the record itself.
    done, _, _ = run_consume(
        tmp_path,
        labels=f"{RECHECK_LABEL}\n",
        delete_status=1,
        delete_body="gh: Server Error (HTTP 500)",
        github_env=False,
    )
    assert done.returncode == 0, done.stderr
    assert f"::error::{RECHECK_LABEL} label removal failed" in done.stdout


def test_an_unreadable_label_set_falls_back_to_trying_the_delete(
    tmp_path: Path,
) -> None:
    # A read that failed says nothing about whether the label is there, so the
    # script must not conclude "absent" from it.
    done, deletes, _ = run_consume(tmp_path, labels="", read_status=502)
    assert done.returncode == 0, done.stderr
    assert deletes == [f"repos/o/r/issues/8/labels/{RECHECK_LABEL}"]
