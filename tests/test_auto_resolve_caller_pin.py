"""The caller's `uses:` pin must satisfy the call the caller makes.

PROBLEM CLASS — a caller passes an input, a secret or a permission ceiling that
the SHA it pins does not declare. GitHub rejects the call before it creates a
single job: the run ends `startup_failure`, so no step logs anything, no check
reports, and the workflow looks idle rather than broken. Observed 2026-08-19:
the caller gained `debug:` in the same commit that declared the input on
auto-resolve.yaml, the pin stayed on an older SHA, and every one of the next 130
runs died at startup while the resolver appeared to be running.

The pin cannot name the commit that introduces an input, so the two land apart:
declare the input on auto-resolve.yaml first, then advance the pin and pass it.
These assertions read the PINNED copy, not the working tree, because the working
tree is the copy that is always ahead.
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests._helpers import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))
# pylint: disable=wrong-import-position  # must follow the sys.path insert above
from gen_caller_permissions import LEVELS, job_permissions, union  # noqa: E402

CALLER = REPO_ROOT / ".github" / "workflows" / "auto-resolve-conflicts.yaml"
CALLEE_PATH = ".github/workflows/auto-resolve.yaml"
CALLER_JOB = "resolve"


def _caller_job() -> dict:
    return yaml.safe_load(CALLER.read_text(encoding="utf-8"))["jobs"][CALLER_JOB]


def _pinned_sha() -> str:
    """The SHA on the caller job's `uses:` line."""
    repo_and_path, _, ref = _caller_job()["uses"].partition("@")
    assert repo_and_path.endswith(CALLEE_PATH), (
        f"{CALLER}: job `{CALLER_JOB}` calls {repo_and_path}, not {CALLEE_PATH}"
    )
    assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), (
        f"{CALLER}: job `{CALLER_JOB}` pins `{ref}`, which is not a commit SHA"
    )
    return ref


@pytest.fixture(name="pinned", scope="module")
def _pinned() -> dict:
    """auto-resolve.yaml as of the pinned commit.

    A missing object fails rather than skips: a clone that cannot read the pin
    cannot verify the call either, and a green meaning "could not look" is the
    failure this module exists to catch.
    """
    sha = _pinned_sha()
    done = subprocess.run(
        ["git", "show", f"{sha}:{CALLEE_PATH}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, (
        f"cannot read {CALLEE_PATH} at the pinned {sha}: {done.stderr.strip()}. "
        "Check out full history (`fetch-depth: 0`), or advance the pin to a "
        "commit this clone contains."
    )
    return yaml.safe_load(done.stdout)


def _workflow_call(doc: dict) -> dict:
    # `on:` parses as the boolean True under YAML 1.1, which is what PyYAML
    # implements; read both spellings rather than betting on the loader.
    return doc.get("on", doc.get(True))["workflow_call"]


@pytest.mark.parametrize("block", ["with", "secrets"])
def test_the_pin_and_the_call_agree_on_every_key(pinned: dict, block: str) -> None:
    """RED when the caller passes a key the pinned copy does not declare, or omits
    one the pinned copy requires — either ends every run in `startup_failure`
    before any job starts."""
    declares = {"with": "inputs", "secrets": "secrets"}[block]
    passed = set(_caller_job()[block])
    declarations = _workflow_call(pinned).get(declares) or {}
    assert passed, f"read no `{block}:` keys — this assertion would pass over nothing"
    assert passed <= set(declarations), (
        f"undeclared at the pin: {sorted(passed - set(declarations))}"
    )
    required = {
        key
        for key, spec in declarations.items()
        if isinstance(spec, dict) and spec.get("required")
    }
    assert required <= passed, (
        f"required at the pin but not passed: {sorted(required - passed)}"
    )


def test_the_caller_ceiling_covers_the_pinned_jobs(pinned: dict) -> None:
    """RED when the pinned copy's jobs ask for a scope the caller does not grant.

    The committed ceiling is generated from the WORKING TREE's auto-resolve.yaml
    (gen_caller_permissions.py), so a pin whose jobs want a scope the tree has
    since dropped satisfies that generator and still fails the call.
    """
    granted = _caller_job()["permissions"]
    callee = Path(CALLEE_PATH)
    short = {
        scope: {"needed": level, "granted": granted.get(scope, "none")}
        for scope, (level, _holders) in union(
            job_permissions(pinned, callee), callee
        ).items()
        if LEVELS[level] > LEVELS.get(granted.get(scope, "none"), 0)
    }
    assert not short, f"ceiling below the pinned jobs: {short}"
