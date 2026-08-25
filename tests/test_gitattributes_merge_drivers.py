"""`.gitattributes` routes each file type to the merge driver that is safe for it.

covers: .gitattributes

The three blocks are ordered, and git resolves `merge` by the LAST matching
pattern, so a reordering silently re-enables a driver that drops content.
This asks `git check-attr` — git's own reader — instead of grepping the file,
which is what makes it survive a reorder rather than a rewording.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
)

# Each row is (path, expected `merge` attribute).
#
# `text` is the built-in line merge, bound deliberately: mergiraf v0.18.0 drops
# one side inside a YAML block scalar and duplicates a TOML table, both while
# reporting the merge solved.
# `unset` is the `-merge` lockfile rule, which refuses any line merge.
# `mergiraf` is the structural merge, safe for the types left on it.
CASES = [
    ("a.yaml", "text"),
    ("a.yml", "text"),
    (".github/workflows/ci.yaml", "text"),
    ("a.toml", "text"),
    ("pyproject.toml", "text"),
    ("uv.lock", "unset"),
    ("pnpm-lock.yaml", "unset"),
    ("package-lock.json", "unset"),
    ("yarn.lock", "unset"),
    ("Cargo.lock", "unset"),
    ("poetry.lock", "unset"),
    ("go.sum", "unset"),
    ("a.py", "mergiraf"),
    ("a.json", "mergiraf"),
    ("a.ts", "mergiraf"),
    ("a.rs", "mergiraf"),
]


def _merge_attr(path: str) -> str:
    """The `merge` attribute git itself resolves for PATH."""
    out = subprocess.run(
        ["git", "check-attr", "merge", "--", path],
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
        text=True,
    ).stdout.strip()
    # `git check-attr` prints `<path>: merge: <value>`, and a path may contain
    # a colon, so split from the RIGHT.
    return out.rsplit(": ", 1)[1]


@pytest.mark.parametrize(("path", "expected"), CASES)
def test_merge_driver_binding(path: str, expected: str) -> None:
    assert _merge_attr(path) == expected


def test_lockfiles_outrank_every_merge_driver() -> None:
    """A lockfile that also matches a driver pattern still refuses a line merge.

    `pnpm-lock.yaml` matches `*.yaml` and `package-lock.json` matches `*.json`,
    so this fails the moment the `-merge` block stops being last.
    """
    for lockfile in ("pnpm-lock.yaml", "package-lock.json", "go.sum"):
        assert _merge_attr(lockfile) == "unset"
