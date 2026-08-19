"""setup.sh's mergiraf leg — the adopt-the-template installer's half of the
merge-driver wiring.

The installer itself is stubbed: the real one downloads a pinned tarball from
Codeberg, which a unit test must not depend on. What is driven for real is
setup.sh's decision — whether it calls the installer, where it installs, and
whether it reports a driver that never got bound.
"""

import os
import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

SETUP = REPO_ROOT / "setup.sh"

# The installer, as setup.sh sees it. Each body ends in a different post-state.
REGISTERS = 'git config merge.mergiraf.driver "stub-driver"\n'
FAILS = "exit 1\n"
# install-mergiraf.sh's own not-a-work-tree arm: the binary is installed, the
# driver is not bound, and it exits 0.
SUCCEEDS_WITHOUT_REGISTERING = "exit 0\n"


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A throwaway repository holding setup.sh and nothing setup.sh's other legs
    act on — no package.json, no uv.lock — so only the mergiraf leg runs."""
    # Not under-provisioning: setup.sh installs nothing off Linux/x86_64 because
    # the pinned asset is linux_amd64, so there is no install to assert.
    if (os.uname().sysname, os.uname().machine) != ("Linux", "x86_64"):
        pytest.skip("setup.sh installs mergiraf only on Linux/x86_64")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "setup.sh").write_bytes(SETUP.read_bytes())
    (tmp_path / ".github" / "scripts").mkdir(parents=True)
    return tmp_path


def run_setup(
    sandbox: Path, installer_body: str, dest_on_path: bool = True
) -> subprocess.CompletedProcess:
    installer = sandbox / ".github" / "scripts" / "install-mergiraf.sh"
    installer.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$1" >>installer-calls\n{installer_body}',
        encoding="utf-8",
    )
    installer.chmod(0o755)
    home = sandbox / "home"
    home.mkdir(exist_ok=True)
    path = os.environ["PATH"]
    if dest_on_path:
        path = f"{home / '.local' / 'bin'}{os.pathsep}{path}"
    return subprocess.run(
        ["bash", "setup.sh"],
        cwd=sandbox,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": path,
            # A host with a global merge.mergiraf.driver — mergiraf's own setup
            # docs register one — would answer for this sandbox otherwise.
            "GIT_CONFIG_GLOBAL": str(sandbox / "gitconfig-global"),
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )


def registered_driver(sandbox: Path) -> str:
    result = subprocess.run(
        ["git", "config", "--get", "merge.mergiraf.driver"],
        cwd=sandbox,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": str(sandbox / "gitconfig-global"),
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )
    return result.stdout.strip()


def test_setup_registers_the_merge_driver(sandbox: Path) -> None:
    result = run_setup(sandbox, REGISTERS)

    assert result.returncode == 0, result.stderr
    assert registered_driver(sandbox) == "stub-driver"
    # Under $HOME, never a root-owned /usr/local/bin: that destination would send
    # the installer through `sudo install` and stall setup on a password prompt.
    bindir = sandbox / "home" / ".local" / "bin"
    assert (sandbox / "installer-calls").read_text(encoding="utf-8").splitlines() == [
        str(bindir)
    ]
    assert bindir.is_dir()


def test_setup_skips_the_install_when_the_destination_is_not_on_path(
    sandbox: Path,
) -> None:
    """install-mergiraf.sh's post-condition is that bare `mergiraf` resolve into
    the directory it installed to, so calling it with a destination off PATH
    downloads the tarball and then refuses — on every run. Naming that cause is
    the only outcome that tells the user what to change."""
    result = run_setup(sandbox, REGISTERS, dest_on_path=False)

    assert result.returncode == 0, result.stderr
    assert "is not on PATH" in result.stderr
    assert not (sandbox / "installer-calls").exists()
    assert registered_driver(sandbox) == ""


@pytest.mark.parametrize(
    "installer_body, expected_warning",
    [
        (FAILS, "mergiraf install failed"),
        (SUCCEEDS_WITHOUT_REGISTERING, "merge.mergiraf.driver is unset"),
    ],
    ids=["installer-fails", "installer-succeeds-without-registering"],
)
def test_setup_completes_and_warns_when_no_driver_is_bound(
    sandbox: Path, installer_body: str, expected_warning: str
) -> None:
    """An adopter without mergiraf still gets a configured checkout — it merges
    as it did before .gitattributes named the driver, and is told so."""
    result = run_setup(sandbox, installer_body)

    assert result.returncode == 0, result.stderr
    assert expected_warning in result.stderr
    assert registered_driver(sandbox) == ""
    assert "Setup complete" in result.stdout


def test_a_global_driver_does_not_answer_for_this_checkout(sandbox: Path) -> None:
    """install-mergiraf.sh binds the driver locally and nowhere else, so the
    post-condition must read the local scope. mergiraf's own setup docs tell users
    to register a global one, and it would otherwise silence the warning while
    merges ran through a binary this run never verified."""
    (sandbox / "gitconfig-global").write_text(
        '[merge "mergiraf"]\n\tdriver = global-driver\n', encoding="utf-8"
    )

    result = run_setup(sandbox, SUCCEEDS_WITHOUT_REGISTERING)

    assert result.returncode == 0, result.stderr
    assert "merge.mergiraf.driver is unset" in result.stderr
