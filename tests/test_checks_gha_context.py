"""Tests for `.github/scripts/checks/gha-context-members.py`.

The check exists because GitHub evaluates an undefined context member to the
empty string rather than to an error, so the cases below pair each flagged shape
with the legitimate shape closest to it: a real member, the open-ended webhook
payload, a caller-defined context, and the same text inside a comment.
"""

import importlib.util
import textwrap

import pytest

from tests._helpers import REPO_ROOT

_CHECK = REPO_ROOT / ".github" / "scripts" / "checks" / "gha-context-members.py"
_WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _load():
    spec = importlib.util.spec_from_file_location("gha_context_members", _CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load()


def _messages(body: str) -> list[str]:
    return [message for _, message in check.violations(textwrap.dedent(body))]


def test_flags_the_documented_but_empty_reusable_workflow_sha() -> None:
    """The read that killed every resolve run, named with its remedy."""
    found = _messages(
        """
        jobs:
          a:
            steps:
              - run: echo hi
                env:
                  REF: ${{ github.job_workflow_sha }}
        """
    )
    assert len(found) == 1
    assert "job.workflow_sha" in found[0]


def test_accepts_the_member_that_replaces_it() -> None:
    assert not _messages(
        """
        jobs:
          a:
            steps:
              - run: echo hi
                env:
                  REF: ${{ job.workflow_sha }}
        """
    )


def test_flags_a_misspelled_github_member() -> None:
    found = _messages("jobs:\n  a:\n    name: ${{ github.repositry }}\n")
    assert len(found) == 1
    assert "github.repositry" in found[0]


def test_accepts_any_depth_under_the_webhook_payload() -> None:
    """`github.event` is the raw payload, so its shape is the sender's."""
    assert not _messages(
        "jobs:\n  a:\n    name: ${{ github.event.workflow_run.head_branch }}\n"
    )


@pytest.mark.parametrize(
    "expression",
    ["inputs.pr", "vars.AUTO_RESOLVE_DEBUG", "secrets.GITHUB_TOKEN", "env.GH_REPO"],
)
def test_accepts_a_caller_defined_context(expression: str) -> None:
    assert not _messages(f"jobs:\n  a:\n    name: ${{{{ {expression} }}}}\n")


def test_flags_outcome_on_a_needs_read() -> None:
    """`outcome` is a step member; `needs.<id>` has only `result` and `outputs`."""
    found = _messages("jobs:\n  a:\n    if: needs.plan.outcome == 'success'\n")
    assert len(found) == 1
    assert "needs.plan.outcome" in found[0]


def test_accepts_result_on_a_needs_read() -> None:
    assert not _messages("jobs:\n  a:\n    if: needs.plan.result == 'success'\n")


def test_reads_a_bare_if_condition() -> None:
    """GitHub evaluates an unbraced `if:` as an expression, so the check must too."""
    found = _messages("jobs:\n  a:\n    if: runner.osx == 'macOS'\n")
    assert len(found) == 1
    assert "runner.osx" in found[0]


def test_ignores_the_same_text_in_a_comment() -> None:
    """This tree explains the interpolation risk by writing `${{ }}` in prose."""
    assert not _messages(
        """
        jobs:
          a:
            # Never splice ${{ github.repositry }} into a run block.
            name: fine
        """
    )


def test_the_annotation_exempts_the_line() -> None:
    assert not _messages(
        "jobs:\n"
        "  a:\n"
        "    name: ${{ github.repositry }} # allow-gha-context: a new member\n"
    )


def test_reports_the_line_inside_a_block_scalar() -> None:
    found = check.violations(
        "jobs:\n  a:\n    run: |\n      one\n      ${{ job.oops }}\n"
    )
    assert [lineno for lineno, _ in found] == [5]


def test_this_repository_is_clean() -> None:
    """The check accepts every workflow here, so a hit is a real finding."""
    for workflow in sorted(_WORKFLOWS.glob("*.yaml")):
        assert not check.violations(workflow.read_text(encoding="utf-8")), workflow
