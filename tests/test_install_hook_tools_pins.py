"""install-hook-tools.sh provisions the CALLER's hook toolchain, so it reads the
CALLER's pins.

PROBLEM CLASS — this script runs inside the resolver's own checkout, so every
relative path it resolves lands in THIS repository. The pins it needs live in the
repository being resolved for. Reading the local copy is silent when the two
repositories happen to pin the same name, and cryptic when they do not: the
resolver's `.github/tool-versions.sh` pins no shellcheck, so `set -u` aborted with
`SHELLCHECK_PY_VERSION: unbound variable` naming a line in the wrong repository.

The script runs here with `uv`, `go` and `python3` replaced by PATH shims that
record their arguments, so what these tests read is which file the versions came
from.
"""

import os
import shutil
import subprocess

import pytest

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "resolver" / "auto-resolve" / "install-hook-tools.sh"

CALLER_SHELLCHECK = "9.9.9.9"
CALLER_SHFMT = "v9.9.9"

_UV_SHIM = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "uv $*" >>"$SHIM_RECORD"
install -d "$HOME/.local/bin"
printf '#!/bin/sh\\necho stub\\n' >"$HOME/.local/bin/shellcheck"
chmod +x "$HOME/.local/bin/shellcheck"
"""

_GO_SHIM = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "go $*" >>"$SHIM_RECORD"
install -d "$GOBIN"
printf '#!/bin/sh\\necho stub\\n' >"$GOBIN/shfmt"
chmod +x "$GOBIN/shfmt"
"""

# hook-py-specs.py needs a real interpreter; the installs and import probes must not
# touch this machine. Dispatching on the argument shape keeps the parse real and the
# side effects stubbed.
_PYTHON_SHIM = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "python3 $*" >>"$SHIM_RECORD"
case "${1:-}" in
-m | -c) exit 0 ;;
*) exec "$REAL_PYTHON" "$@" ;;
esac
"""

_CALLER_PYPROJECT = """[project]
name = "caller"
version = "0"
dependencies = ["agent-sanitizer==1.0.0"]

[project.optional-dependencies]
dev = ["pyyaml==6.0.2", "pathspec==0.12.1"]
"""


def _tool_versions(shellcheck: str | None, shfmt: str | None) -> str:
    lines = ["# shellcheck shell=bash"]
    if shellcheck is not None:
        lines.append(f"SHELLCHECK_PY_VERSION={shellcheck}")
    if shfmt is not None:
        lines.append(f"SHFMT_VERSION={shfmt}")
    return "\n".join(lines) + "\n"


class _Harness:
    """A caller checkout, a shimmed PATH, and one invocation of the script."""

    def __init__(self, tmp_path):
        self.caller = tmp_path / "base"
        (self.caller / ".github").mkdir(parents=True)
        (self.caller / "pyproject.toml").write_text(_CALLER_PYPROJECT, encoding="utf-8")
        self.tool_versions = self.caller / ".github" / "tool-versions.sh"
        self.tool_versions.write_text(
            _tool_versions(CALLER_SHELLCHECK, CALLER_SHFMT), encoding="utf-8"
        )

        self.shims = tmp_path / "shims"
        self.shims.mkdir()
        for name, body in (
            ("uv", _UV_SHIM),
            ("go", _GO_SHIM),
            ("python3", _PYTHON_SHIM),
        ):
            shim = self.shims / name
            shim.write_text(body, encoding="utf-8")
            shim.chmod(0o755)

        self.home = tmp_path / "home"
        self.home.mkdir()
        self.record = tmp_path / "record"
        self.record.touch()
        self.github_path = tmp_path / "github_path"
        self.github_path.touch()

    def run(self) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.shims}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "REAL_PYTHON": shutil.which("python3"),
            "SHIM_RECORD": str(self.record),
            "BASE_REPO_ROOT": str(self.caller),
            "GITHUB_PATH": str(self.github_path),
        }
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def recorded(self) -> str:
        return self.record.read_text(encoding="utf-8")


@pytest.fixture
def harness(tmp_path):
    return _Harness(tmp_path)


def test_the_binary_pins_come_from_the_caller_not_the_resolver(harness) -> None:
    """RED when the script sources its own repository's tool-versions.sh: that file
    pins neither name, so the run dies under `set -u` before installing anything."""
    result = harness.run()
    assert result.returncode == 0, result.stderr
    recorded = harness.recorded()
    assert f"shellcheck-py=={CALLER_SHELLCHECK}" in recorded
    assert f"mvdan.cc/sh/v3/cmd/shfmt@{CALLER_SHFMT}" in recorded


def test_a_caller_without_the_pins_file_fails_naming_the_path(harness) -> None:
    """RED when the missing file reaches `source` — bash then reports a path inside
    the resolver's temporary clone, which is not where the reader must add it."""
    harness.tool_versions.unlink()
    result = harness.run()
    assert result.returncode == 1
    assert str(harness.tool_versions) in result.stdout + result.stderr


def test_a_caller_missing_one_pin_fails_naming_that_pin(harness) -> None:
    """RED when the unset name reaches the install command — `set -u` then aborts
    with a line number in this repository and no mention of the caller's file."""
    harness.tool_versions.write_text(
        _tool_versions(None, CALLER_SHFMT), encoding="utf-8"
    )
    result = harness.run()
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "SHELLCHECK_PY_VERSION" in combined
    assert str(harness.tool_versions) in combined
