"""The resolver clone is pinned to the sha the caller's `uses:` names.

covers: .github/scripts/resolver-ref.py

The resolver's code runs beside this repository's tokens, so which version it is
is the security control. A clone naming no ref takes the remote's HEAD, which is
upstream code the consumer never accepted.
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
)
sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))

_MODULE = REPO_ROOT / ".github" / "scripts" / "resolver-ref.py"


def _resolver_ref(root: Path) -> str:
    """Run the reader as a process, the way every job does."""
    return subprocess.run(
        [sys.executable, str(_MODULE), str(root)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def _caller(root: Path, uses: str) -> None:
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "auto-resolve-conflicts.yaml").write_text(
        yaml.safe_dump({"jobs": {"resolve": {"uses": uses}}}), encoding="utf-8"
    )


def test_it_reads_this_repository_s_own_pin():
    """The real caller, so a pin that stops parsing is caught here."""
    ref = _resolver_ref(REPO_ROOT)
    assert ref, "the caller names no resolver ref"
    doc = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/auto-resolve-conflicts.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert doc["jobs"]["resolve"]["uses"].endswith("@" + ref)


def test_every_workflow_that_clones_the_resolver_pins_it(tmp_path: Path):
    """An unpinned clone takes the remote's HEAD.

    Two of these jobs hold a write-scoped token, so this is the check that stops
    a consumer executing upstream code it never accepted.
    """
    for name in ("pr-meta.yaml", "claude-review.yaml"):
        text = (REPO_ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            if "git clone" not in line or "RESOLVER_REPOSITORY" not in line:
                continue
            window = "\n".join(text.splitlines()[line_no - 1 : line_no + 4])
            assert "--no-checkout" in window and "FETCH_HEAD" in window, (
                f"{name}:{line_no} clones the resolver without checking out a "
                "pinned ref"
            )


@pytest.mark.parametrize(
    "uses",
    [
        "owner/repo/.github/workflows/auto-resolve.yaml",
        "owner/repo/.github/workflows/auto-resolve.yaml@",
        "owner/repo/.github/workflows/auto-resolve.yaml@-upload-pack=evil",
    ],
    ids=["no-ref", "empty-ref", "option-shaped-ref"],
)
def test_an_unusable_ref_refuses(tmp_path: Path, uses: str):
    """An absent ref makes the clone take HEAD, and a leading `-` is read by
    git's option parser rather than as a ref."""
    _caller(tmp_path, uses)
    done = subprocess.run(
        [sys.executable, str(_MODULE), str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0, done.stdout
    assert "no usable" in done.stderr
