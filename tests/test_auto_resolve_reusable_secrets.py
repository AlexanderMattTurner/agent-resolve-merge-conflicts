"""The reusable resolver's `secrets:` declaration must cover what it reads.

PROBLEM CLASS — a secret a called workflow reads but does not DECLARE arrives
empty. It raises no error at any layer: the expression evaluates, the env var is
set to "", and the credential ladder reads the rung as dead and walks past it. A
consumer repository that configured the secret correctly then watches its paid
rungs silently do nothing, and the run reports "every credential billed nothing"
instead of naming the omission.

The declaration is also the contract a consumer configures against, so the
caller in this repository has to pass every declared name — a caller that
passes a subset is the same silent-empty failure with a different cause.
"""

import pathlib
import re

import pytest
import yaml

from tests._helpers import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
REUSABLE = WORKFLOWS / "auto-resolve.yaml"
CALLER = WORKFLOWS / "auto-resolve-conflicts.yaml"


def _trigger(doc: dict) -> dict:
    """`on:` parses as the boolean True under YAML 1.1, which is what PyYAML
    implements; read both spellings rather than betting on the loader."""
    return doc.get("on", doc.get(True)) or {}


def _reusable_workflows() -> list[pathlib.Path]:
    """Every workflow a consumer can call. Derived from the directory, so a
    reusable workflow added later inherits this contract instead of shipping
    unguarded — which is how the second one arrived."""
    found = [
        path
        for path in sorted(WORKFLOWS.glob("*.yaml"))
        if "workflow_call" in _trigger(yaml.safe_load(path.read_text(encoding="utf-8")))
    ]
    assert found, (
        "found no workflow_call workflows — every case below would pass over nothing"
    )
    return found


# Injected by Actions into every workflow, so it is never declared or passed.
AUTOMATIC = {"GITHUB_TOKEN"}

SECRET_REF = re.compile(r"\bsecrets\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)")


def _declared(workflow: pathlib.Path = REUSABLE) -> set[str]:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    return set((_trigger(doc)["workflow_call"].get("secrets") or {}).keys())


def _referenced(workflow: pathlib.Path = REUSABLE) -> set[str]:
    """Every `secrets.NAME` the reusable workflow reads, minus the automatic one.

    Read from the text, not the parsed tree: a reference can sit anywhere an
    expression is allowed — a step `env:` value, a multi-line `>-` chain of
    conditionals, a `with:` input — and walking the tree for all of those is a
    second parser for a syntax the regex already covers exactly.
    """
    return set(SECRET_REF.findall(workflow.read_text(encoding="utf-8"))) - AUTOMATIC


@pytest.mark.parametrize("workflow", _reusable_workflows(), ids=lambda p: p.name)
def test_every_secret_a_reusable_workflow_reads_is_declared(
    workflow: pathlib.Path,
) -> None:
    """RED when a step gains a `secrets.X` the `workflow_call` block omits — the
    case where X reaches the runner as an empty string and reads as a dead
    credential."""
    referenced = _referenced(workflow)
    declared = _declared(workflow)
    assert referenced <= declared, f"undeclared: {sorted(referenced - declared)}"


@pytest.mark.parametrize("workflow", _reusable_workflows(), ids=lambda p: p.name)
def test_no_declared_secret_is_unread(workflow: pathlib.Path) -> None:
    """The other direction: a declared name nothing reads is a contract entry a
    consumer configures for no effect."""
    declared = _declared(workflow)
    assert declared <= _referenced(workflow), (
        f"declared but unread: {sorted(declared - _referenced(workflow))}"
    )


def test_the_caller_passes_every_declared_secret() -> None:
    """A `secrets:` mapping that omits a name is indistinguishable at run time
    from a repository that never set it, so the omission has to fail here."""
    doc = yaml.safe_load(CALLER.read_text(encoding="utf-8"))
    secrets = doc["jobs"]["resolve"].get("secrets") or {}
    # `inherit` parses as a scalar string, not a mapping. Naming it here is what
    # keeps the wholesale hand-over a FAILURE rather than an AttributeError.
    assert isinstance(secrets, dict), (
        f"caller passes `secrets: {secrets}` instead of a named list"
    )
    assert set(secrets) == _declared(), (
        f"caller omits {sorted(_declared() - set(secrets))}; "
        f"passes undeclared {sorted(set(secrets) - _declared())}"
    )
