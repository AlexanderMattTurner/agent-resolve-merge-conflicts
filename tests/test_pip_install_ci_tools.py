"""How `pip-install-ci-tools.sh` installs the resolver's own `pre-commit`.

A pip install as a non-root runner lands in `~/.local`, and its `pre-commit`
script imports the module only while HOME is unchanged. A caller's test that
moves HOME then dies with `ModuleNotFoundError: No module named 'pre_commit'`,
which the resolver reads as a finding about the merge. A uv tool keeps its own
venv, so the script installs pre-commit that way and puts uv's bin dir on PATH.
Both CLIs are shims, so no test needs the network.
"""

import re
import subprocess

from tests._resolver_helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "resolver" / "pip-install-ci-tools.sh"
TOOL_VERSIONS = REPO_ROOT / ".github" / "tool-versions.sh"


def _run_precommit_install(tmp_path, github_path):
    uv_bin = tmp_path / "uv-bin"
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    log = tmp_path / "calls.log"
    for name, body in (
        ("uv", f'echo "uv $*" >>"{log}"\n[[ "$*" != "tool dir --bin" ]] || echo "{uv_bin}"'),
        ("python3", f'echo "python3 $*" >>"{log}"'),
    ):
        shim = shim_dir / name
        shim.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
        shim.chmod(0o755)
    env = {"PATH": f"{shim_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    if github_path is not None:
        env["GITHUB_PATH"] = str(github_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), "pre-commit"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return result, calls, uv_bin


def test_pre_commit_is_a_uv_tool_on_github_path(tmp_path):
    """pre-commit is installed at the pinned version with `uv tool install`,
    uv's bin dir is appended to GITHUB_PATH, and pip is never asked for it."""
    pin = re.search(r"^PRE_COMMIT_VERSION=(\S+)$", TOOL_VERSIONS.read_text(), re.M)[1]
    github_path = tmp_path / "github_path"
    result, calls, uv_bin = _run_precommit_install(tmp_path, github_path)
    assert result.returncode == 0, result.stderr
    assert f"uv tool install --quiet pre-commit=={pin}" in calls
    assert github_path.read_text(encoding="utf-8").splitlines() == [str(uv_bin)]
    assert "pip" not in calls


def test_pre_commit_install_without_github_path_fails_before_installing(tmp_path):
    """No GITHUB_PATH means later steps could not find the binary, so the script
    refuses and names the variable instead of installing something unreachable."""
    result, calls, _ = _run_precommit_install(tmp_path, None)
    assert result.returncode != 0
    assert "GITHUB_PATH" in result.stderr
    assert "tool install" not in calls
