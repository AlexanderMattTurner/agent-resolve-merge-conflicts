"""The base-branch scan reaches a pull request whatever branch it targets.

PROBLEM CLASS — a trigger that names ONE branch in a workflow whose subject is
every branch. A pull request against `release/1.2` or `develop` is conflicted by
a push to that branch, and that push emits no `pull_request` event: a
`push: branches: [main]` filter therefore starts no scan and no labeling run, so
the conflict waits for the scheduled backstop or, where an adopter turned the
backstop off, forever.
"""

# covers: .github/workflows/auto-resolve-conflicts.yaml
# covers: .github/workflows/pr-meta-privileged.yaml
# covers: .github/workflows/auto-resolve.yaml

import re
from pathlib import Path

import pytest
import yaml

from tests._gha_if import evaluate
from tests._helpers import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
RESOLVER = WORKFLOWS / "auto-resolve-conflicts.yaml"
LABELER = WORKFLOWS / "pr-meta-privileged.yaml"

# Branches a repository points pull requests at besides its default one.
NON_DEFAULT_BASES = ["develop", "release/1.2", "team/infra/base"]


def _push_trigger(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML reads the bare `on:` key as the boolean True.
    return doc[True]["push"] or {}


def _glob_matches(pattern: str, branch: str) -> bool:
    """GitHub's branch glob for the forms these workflows use: `**` spans path
    separators, `*` stops at one. Any other metacharacter raises, so an
    unhandled pattern is loud instead of silently deciding the case. A leading
    `!` belongs to the filter, not the glob, so the caller strips it first."""
    if set(pattern) & set("?+[]"):
        raise ValueError(f"unhandled branch-filter glob: {pattern}")
    regex = "".join(
        ".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
        for part in re.split(r"(?P<glob>\*\*|\*)", pattern)
    )
    return re.fullmatch(regex, branch) is not None


def _fires_on_push_to(path: Path, branch: str) -> bool:
    """Whether a push to `branch` starts a run of this workflow."""
    push = _push_trigger(path)
    ignored = push.get("branches-ignore")
    if ignored is not None:
        return not any(_glob_matches(p, branch) for p in ignored)
    patterns = push.get("branches")
    if patterns is None:
        return True
    # GitHub takes the LAST pattern that matches, so a `!` exclusion after a
    # wildcard removes what the wildcard admitted.
    fires = False
    for pattern in patterns:
        excludes = pattern.startswith("!")
        if _glob_matches(pattern.removeprefix("!"), branch):
            fires = not excludes
    return fires


@pytest.mark.parametrize("workflow", [RESOLVER, LABELER], ids=["resolver", "labeler"])
@pytest.mark.parametrize("branch", NON_DEFAULT_BASES)
def test_a_push_to_any_base_branch_starts_a_scan(workflow: Path, branch: str) -> None:
    assert _fires_on_push_to(workflow, branch), (
        f"{workflow.name} must scan after a push to `{branch}`: a pull request "
        "based there is conflicted by that push and by no event of its own."
    )


@pytest.mark.parametrize("workflow", [RESOLVER, LABELER], ids=["resolver", "labeler"])
def test_a_release_tag_push_starts_no_scan(workflow: Path) -> None:
    """A `push:` with no `branches:` at all fires on TAGS too, and this
    repository pushes a tag per release. `branches: ["**"]` is every branch and
    no tag."""
    assert "branches" in _push_trigger(workflow), (
        f"{workflow.name}'s push trigger must keep a `branches:` filter, or "
        "every release tag starts a scan of its own."
    )


@pytest.mark.parametrize("workflow", [RESOLVER, LABELER], ids=["resolver", "labeler"])
def test_a_merge_queue_branch_push_starts_no_scan(workflow: Path) -> None:
    """The merge queue pushes an ephemeral `gh-readonly-queue/<base>/pr-<n>-<sha>`
    branch per entry. No pull request targets one, so a scan of it buys nothing
    and can still relay a paid dispatch."""
    assert not _fires_on_push_to(workflow, "gh-readonly-queue/main/pr-119-abc123"), (
        f"{workflow.name} must skip the merge queue's own branches."
    )


def test_the_land_job_names_the_default_branch_for_its_self_dispatches() -> None:
    """`land.sh` and `continue-partial.sh` both exit non-zero without
    `DISPATCH_REF`, and both suites supply it from their own fixtures — so only a
    read of the workflow catches the job that stops setting it."""
    resolver = yaml.safe_load(
        (WORKFLOWS / "auto-resolve.yaml").read_text(encoding="utf-8")
    )
    assert (
        "github.event.repository.default_branch"
        in resolver["jobs"]["land"]["env"]["DISPATCH_REF"]
    ), (
        "the land job must name the default branch for the race retry and the "
        "carry dispatch; without this entry both scripts exit non-zero."
    )


_RESOLVER_DOC = yaml.safe_load(RESOLVER.read_text(encoding="utf-8"))
# Every trigger the workflow declares, so a new one must decide its own case here.
TRIGGERS = sorted(_RESOLVER_DOC[True])
FOUND_ONE = '[{"number":168}]'


def _discover_job() -> dict:
    return _RESOLVER_DOC["jobs"]["discover"]


def _dispatch_step() -> dict:
    """The `discover` step that re-fires a scan as a `workflow_dispatch`."""
    steps = [
        s for s in _discover_job()["steps"] if "gh workflow run" in s.get("run", "")
    ]
    assert len(steps) == 1, (
        "discover must carry exactly one dispatch step: a push or scheduled scan "
        "reaches the paid resolve job by no other route."
    )
    return steps[0]


def _admits(condition: str, event: str, prs: str = FOUND_ONE) -> bool:
    """Whether `condition` admits `event`, given what the scan found."""
    return evaluate(
        condition,
        {
            # A non-`pull_request` event carries neither member, and the job gate
            # reads both; empty is what the expression sees there.
            "github": {
                "event_name": event,
                "event": {"action": "", "label": {"name": ""}},
            },
            "steps": {"discover": {"outputs": {"prs": prs}}},
            "needs": {"discover": {"outputs": {"prs": prs}}},
            "vars": {"AUTO_RESOLVE_DISABLED": "", "AUTO_RESOLVE_SCHEDULE_DISABLED": ""},
        },
    )


def _dispatches_on(event: str, prs: str = FOUND_ONE) -> bool:
    """Whether a scan actually re-dispatches — BOTH gates, not just the step's.

    The step's `if:` is only half the guard: the run has to reach the step at
    all, and the `discover` job carries its own `if:` that a change could narrow
    without touching the step."""
    return _admits(_discover_job()["if"], event, prs) and _admits(
        _dispatch_step()["if"], event, prs
    )


def test_only_the_unresolvable_events_dispatch() -> None:
    """`push` and `schedule` run under an event claude-code-action rejects, so
    each re-fires the scan as a `workflow_dispatch`. The step now lives in
    `discover`, which every trigger starts, so this gate is the whole loop guard:
    admitting `workflow_dispatch` would make each dispatched run start another,
    without bound."""
    assert {e for e in TRIGGERS if _dispatches_on(e)} == {"push", "schedule"}


def test_every_trigger_either_dispatches_or_resolves() -> None:
    """The two gates PARTITION the triggers, and they sit in different jobs with
    nothing else holding them together. An event that satisfies neither leaves a
    conflict nothing acts on; one that satisfies both pays two runs for it."""
    resolve = _RESOLVER_DOC["jobs"]["resolve"]["if"]
    for event in TRIGGERS:
        assert _dispatches_on(event) != _admits(resolve, event), (
            f"`{event}` must either re-dispatch or resolve, and does neither or both."
        )


def test_the_dispatch_step_reads_a_scan_that_already_ran() -> None:
    """The gate reads `steps.discover.outputs.prs`, which is EMPTY for any step
    placed before the scan that sets it — so this job's step order decides
    whether a push ever dispatches. The `relay` job this step replaced could not
    express the bug: `needs: discover` ordered it by construction."""
    steps = _discover_job()["steps"]
    scan = next(i for i, s in enumerate(steps) if s.get("id") == "discover")
    assert scan < steps.index(_dispatch_step())


@pytest.mark.parametrize("event", ["push", "schedule"])
@pytest.mark.parametrize("prs", ["", "[]"], ids=["unset", "empty"])
def test_a_scan_that_found_nothing_dispatches_nothing(event: str, prs: str) -> None:
    """A dispatched run costs a runner and a full re-scan, so an empty result
    must not buy one."""
    assert not _dispatches_on(event, prs)


def test_the_push_scan_dispatches_on_the_default_branch() -> None:
    """`workflow_dispatch` runs the workflow file the NAMED ref carries. With
    the scan firing on every branch, dispatching the pushed ref would run that
    branch's own copy of this workflow — one predating the inputs the dispatch
    sends, or one edited on a feature branch — with this repository's secrets."""
    step = _dispatch_step()
    assert "github.event.repository.default_branch" in step["env"]["DISPATCH_REF"]
    assert "${DISPATCH_REF:?" in step["run"], (
        "the dispatch must fail loud on an empty ref, as its two siblings do."
    )
    assert "GITHUB_REF_NAME" not in step["run"], (
        "the dispatch must not name the ref that was pushed."
    )


def test_the_dispatching_job_stages_no_tree_a_pull_request_author_writes() -> None:
    """What licenses `actions: write` on `discover`: every tree it stages is one
    no pull request author can write. `actions/checkout` takes the MERGE REF by
    default on a `pull_request` event — the author's own copy of the scripts this
    job runs — and that author would then reach the scope that dispatches this
    workflow on any ref."""
    discover = _discover_job()
    assert discover["permissions"]["actions"] == "write"
    checkouts = [
        s for s in discover["steps"] if "actions/checkout" in s.get("uses", "")
    ]
    assert checkouts, "discover must stage the default branch it runs its scripts from."
    for step in checkouts:
        assert step["with"]["ref"] == "${{ github.event.repository.default_branch }}", (
            "a checkout in this job must name the default branch: the default ref "
            "is the merge ref, which the pull request author writes."
        )
        assert step["with"]["persist-credentials"] is False, (
            "a checkout in this job must not leave its token in .git/config, where "
            "every later step in an `actions: write` job would reach it."
        )
