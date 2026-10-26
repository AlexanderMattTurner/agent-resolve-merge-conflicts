"""The caller's workflow file, named from `$GITHUB_WORKFLOW_REF`.

The resolver runs inside a caller each repository names itself, so a dispatch
or an artifact pin that spells `auto-resolve-conflicts.yaml` reaches nothing in
a repository whose caller is called anything else.

covers: .github/resolver/_workflow_ref.py
covers: .github/resolver/auto-resolve/continue-partial.sh
"""

import os
import subprocess
from pathlib import Path

import pytest

from tests._resolver_helpers import REPO_ROOT, load_script

workflow_ref = load_script(".github/resolver/_workflow_ref.py")
CONTINUE_PARTIAL = REPO_ROOT / ".github/resolver/auto-resolve/continue-partial.sh"


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("o/r/.github/workflows/my-caller.yaml@refs/heads/main", "my-caller.yaml"),
        ("o/r/.github/workflows/conflicts.yml@refs/tags/v1", "conflicts.yml"),
    ],
)
def test_the_file_is_read_off_the_ref(ref: str, expected: str) -> None:
    assert workflow_ref.workflow_file(ref) == expected


@pytest.mark.parametrize("ref", ["", "o/r@refs/heads/main", "o/r/README.md@x"])
def test_a_ref_with_no_workflow_file_is_refused(ref: str) -> None:
    with pytest.raises(ValueError, match="not a workflow ref"):
        workflow_ref.workflow_file(ref)


def test_the_next_round_dispatches_the_callers_own_file(tmp_path: Path) -> None:
    log = tmp_path / "gh.log"
    gh = tmp_path / "gh"
    gh.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"{log}"\n', encoding="utf-8"
    )
    gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PR": "7",
        "CARRY_CONTINUE": "true",
        "DISPATCH_REF": "main",
        "GITHUB_WORKFLOW_REF": "o/r/.github/workflows/my-caller.yaml@refs/heads/main",
    }
    subprocess.run(["bash", str(CONTINUE_PARTIAL)], env=env, check=True)
    assert log.read_text(encoding="utf-8").startswith("workflow run my-caller.yaml ")
