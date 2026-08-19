"""The hook toolchain's pins come from the CALLING repository, not from this one.

`install-hook-tools.sh` provisions shellcheck and shfmt for the pre-commit hooks
that run over a resolved merge. Those hooks belong to the caller, so their pins
have to be the caller's: a resolver-local pin would run the merge through a
different shellcheck than the caller's own CI. Both tests below stop before any
download, so they need no network and no `uv`/`go` on PATH.
"""

import subprocess

from tests._resolver_helpers import REPO_ROOT

INSTALL = REPO_ROOT / ".github" / "resolver" / "auto-resolve" / "install-hook-tools.sh"
CALLER_PINS = "SHELLCHECK_PY_VERSION=0.11.0.1\nSHFMT_VERSION=v3.13.1\n"


def _run(base_repo_root, env_extra=None):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(base_repo_root),
        "BASE_REPO_ROOT": str(base_repo_root),
    }
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(INSTALL)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_missing_caller_tool_versions_names_the_file(tmp_path):
    """A caller with no `.github/tool-versions.sh` is told which file is absent."""
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "tool-versions.sh does not exist" in result.stdout + result.stderr


def test_the_caller_pin_is_the_version_installed(tmp_path):
    """`uv tool install` names the version the CALLER pinned.

    This repository's own `.github/tool-versions.sh` pins neither name. Sourcing
    it instead of the caller's aborts with `SHELLCHECK_PY_VERSION: unbound
    variable` before `uv` is ever called, which is the break this test holds shut.
    """
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "tool-versions.sh").write_text(
        CALLER_PINS, encoding="utf-8"
    )
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    for name in ("uv", "go"):
        shim = shim_dir / name
        shim.write_text(
            f'#!/usr/bin/env bash\necho "{name} $*" >>"{argv_log}"\n', encoding="utf-8"
        )
        shim.chmod(0o755)
    result = _run(
        tmp_path,
        {
            "PATH": f"{shim_dir}:/usr/bin:/bin",
            "GITHUB_PATH": str(tmp_path / "github_path"),
        },
    )
    combined = result.stdout + result.stderr
    assert "unbound variable" not in combined
    argv = argv_log.read_text(encoding="utf-8")
    assert "uv tool install --quiet shellcheck-py==0.11.0.1" in argv
    assert "go install mvdan.cc/sh/v3/cmd/shfmt@v3.13.1" in argv
    # The shims install nothing, so the run still ends red further down. What
    # this test holds is the pin, not the ending.
    assert result.returncode != 0


def test_a_caller_that_pins_neither_binary_installs_neither(tmp_path):
    """The caller whose shellcheck and shfmt hooks come from pre-commit's own hook
    repositories pins neither, because pre-commit provisions them itself. Demanding
    a pin there killed every resolve in such a repository at this step, before any
    merge — so an unpinned pair installs nothing and says so."""
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "tool-versions.sh").write_text(
        "PRE_COMMIT_VERSION=4.6.1\n", encoding="utf-8"
    )
    result = _run(tmp_path, {"GITHUB_PATH": str(tmp_path / "github_path")})
    combined = result.stdout + result.stderr
    assert "pins neither SHELLCHECK_PY_VERSION nor SHFMT_VERSION" in combined
    assert "is unset" not in combined
    # Neither binary was asked for, so neither missing toolchain is a refusal.
    assert "is not on PATH" not in combined
