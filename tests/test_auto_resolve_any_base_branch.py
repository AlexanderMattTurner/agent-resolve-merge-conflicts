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

import re
from pathlib import Path

import pytest
import yaml

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
    unhandled pattern is loud instead of silently deciding the case."""
    if set(pattern) & set("?+![]"):
        raise ValueError(f"unhandled branch-filter glob: {pattern}")
    regex = "".join(
        ".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
        for part in re.split(r"(\*\*|\*)", pattern)
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
    return any(_glob_matches(p, branch) for p in patterns)


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


def test_the_push_scan_relays_on_the_default_branch() -> None:
    """`workflow_dispatch` runs the workflow file the NAMED ref carries. With
    the scan firing on every branch, relaying against the pushed ref would run
    that branch's own copy of this workflow — one predating the inputs the relay
    sends, or one edited on a feature branch — with this repository's secrets."""
    doc = yaml.safe_load(RESOLVER.read_text(encoding="utf-8"))
    step = doc["jobs"]["relay"]["steps"][0]
    assert "github.event.repository.default_branch" in step["env"]["DISPATCH_REF"]
    assert "${DISPATCH_REF}" in step["run"]
    assert "GITHUB_REF_NAME" not in step["run"], (
        "the relay must not dispatch the ref that was pushed."
    )
