"""The resolver clone is pinned to the sha the caller's `uses:` names.

covers: .github/scripts/resolver-ref.py
covers: .github/scripts/resolver-dir.sh
covers: .github/workflows/auto-resolve-conflicts.yaml
covers: .github/workflows/pr-meta.yaml
covers: .github/workflows/claude-review.yaml

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


_WORKFLOWS = [
    REPO_ROOT / ".github/workflows/auto-resolve-conflicts.yaml",
    REPO_ROOT / ".github/workflows/pr-meta.yaml",
    REPO_ROOT / ".github/workflows/claude-review.yaml",
]


def _holds_a_write_token(job: dict) -> bool:
    """A job that can change the repository, so upstream HEAD must not run in it."""
    perms = job.get("permissions")
    if isinstance(perms, str):
        return perms == "write-all"
    return any(value == "write" for value in (perms or {}).values())


def _unpinned_resolver_clones(doc: dict) -> list[str]:
    """Names the write-scoped jobs that clone the resolver with no ref pinned.

    A read-scoped job may clone the default branch: `discover` does, and its
    whole output is a list of pull request numbers. The same clone under a write
    token runs upstream HEAD beside a credential the consumer never handed it.
    """
    found = []
    for name, job in (doc.get("jobs") or {}).items():
        if not _holds_a_write_token(job):
            continue
        for step in job.get("steps") or []:
            run = step.get("run") or ""
            if "git clone" not in run or "RESOLVER_REPOSITORY" not in run:
                continue
            if "--no-checkout" not in run or "FETCH_HEAD" not in run:
                found.append(name)
    return found


def test_every_clone_of_the_resolver_pins_it():
    """An unpinned clone takes the remote's HEAD.

    Two of the jobs that reach the resolver hold a write-scoped token, so this
    is the check that stops a consumer executing upstream code it never
    accepted. `tests/test_resolver_dir.py` proves the pin behaviourally against
    a local remote; this one keeps a NEW unpinned clone from appearing beside
    it.
    """
    lines = (
        (REPO_ROOT / ".github/scripts/resolver-dir.sh")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    seen = 0
    for line_no, line in enumerate(lines, 1):
        if "git clone" not in line:
            continue
        # The URL can sit on a continuation line, so judge the whole command.
        window = "\n".join(lines[line_no - 1 : line_no + 4])
        if "RESOLVER_REPOSITORY" not in window:
            continue
        seen += 1
        assert "--no-checkout" in window and "FETCH_HEAD" in window, (
            f"resolver-dir.sh:{line_no} clones the resolver without checking "
            "out a pinned ref"
        )
    assert seen, "no resolver clone found — this guard has gone vacuous"

    write_scoped = 0
    for path in _WORKFLOWS:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        write_scoped += sum(_holds_a_write_token(job) for job in doc["jobs"].values())
        assert _unpinned_resolver_clones(doc) == [], (
            f"{path.name} clones the resolver unpinned in a write-scoped job"
        )
    assert write_scoped, "no write-scoped job read — this guard sees nothing"


def test_an_unpinned_clone_in_a_write_scoped_job_is_named():
    """The refusing direction: the shape the workflow scan above must reject."""
    doc = {
        "jobs": {
            "read-only": {
                "permissions": {"contents": "read"},
                "steps": [{"run": 'git clone "https://x/${RESOLVER_REPOSITORY}.git"'}],
            },
            "reporter": {
                "permissions": {"pull-requests": "write"},
                "steps": [{"run": 'git clone "https://x/${RESOLVER_REPOSITORY}.git"'}],
            },
        }
    }
    assert _unpinned_resolver_clones(doc) == ["reporter"]


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
