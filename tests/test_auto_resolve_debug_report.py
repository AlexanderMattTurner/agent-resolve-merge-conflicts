"""The debug report must survive the endings it exists to describe.

PROBLEM CLASS — a resolver run that dies before it clones the resolver publishes
NOTHING: no status comment, no fan-out log, no artifact. Every surface that
would say why is inside the code the dead step was fetching. Thirty consecutive
runs failed that way on 2026-08-18 and the only record was an Actions tab.

So the report job is defined by what it does NOT depend on. It reports on
`always()`, it runs no repository code, and it holds no credential beyond the
one comment it posts. Each test below pins one of those.
"""

import re

import yaml
from lark import Token

from _gha_expression import context_reads, parse_condition
from tests._helpers import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
REUSABLE = WORKFLOWS / "auto-resolve.yaml"
CALLER = WORKFLOWS / "auto-resolve-conflicts.yaml"

JOB = "debug-report"
SECRET_REF = re.compile(r"\bsecrets\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)")


def _jobs(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def _calls(node) -> set[str]:
    """Every function an expression calls, by name."""
    if isinstance(node, Token):
        return set()
    called = {c for child in node.children for c in _calls(child)}
    if node.data == "funcall":
        called.add(node.children[0].value)
    return called


def test_the_report_fires_on_every_ending_the_caller_asked_about() -> None:
    """RED when the gate loses `always()` — the endings worth a report are
    exactly the ones a default gate drops: a first-step death, a cancellation, a
    land job that never ran."""
    condition = parse_condition(_jobs(REUSABLE)[JOB]["if"])
    assert _calls(condition) == {"always"}
    assert context_reads(condition) == {"inputs.debug"}


def test_the_report_runs_nothing_the_resolver_clone_would_have_brought() -> None:
    """RED when a step gains a `uses:` or reads RESOLVER_DIR. Either makes the
    report depend on the checkout whose failure it is there to describe."""
    steps = _jobs(REUSABLE)[JOB]["steps"]
    assert [step for step in steps if "uses" in step] == []
    assert [step for step in steps if "RESOLVER_DIR" in yaml.dump(step)] == []


def test_the_report_holds_no_credential_beyond_its_comment() -> None:
    """RED when the job gains write scope or a resolver secret. It reads an
    untrusted branch's job names into a comment, so it is the wrong place to
    hold anything that can push."""
    job = _jobs(REUSABLE)[JOB]
    assert job["permissions"] == {"actions": "read", "pull-requests": "write"}
    assert set(SECRET_REF.findall(yaml.dump(job))) == {"GITHUB_TOKEN"}


def test_the_caller_hands_the_flag_to_the_resolver() -> None:
    """RED when the caller stops passing `debug` — the input then takes its
    `false` default and no dispatch or repository variable can turn it on."""
    passed = _jobs(CALLER)["resolve"]["with"]["debug"]
    assert "inputs.debug" in passed and "AUTO_RESOLVE_DEBUG" in passed
