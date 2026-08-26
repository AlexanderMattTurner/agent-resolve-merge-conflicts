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
    sandbox: Path, installer_body: str, env_overrides: dict | None = None
) -> subprocess.CompletedProcess:
    installer = sandbox / ".github" / "scripts" / "install-mergiraf.sh"
    installer.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$1" >>installer-calls\n'
        'printf "%s\\n" "$PATH" >>installer-path\n'
        f"{installer_body}",
        encoding="utf-8",
    )
    installer.chmod(0o755)
    home = sandbox / "home"
    home.mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", "setup.sh"],
        cwd=sandbox,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(home),
            # Deliberately WITHOUT $HOME/.local/bin, the case setup.sh has to
            # carry: the installer refuses a destination bare `mergiraf` does not
            # resolve into, so setup.sh must put it there for that call.
            "PATH": os.environ["PATH"],
            # A host with a global merge.mergiraf.driver — mergiraf's own setup
            # docs register one — would answer for this sandbox otherwise.
            "GIT_CONFIG_GLOBAL": str(sandbox / "gitconfig-global"),
            "GIT_CONFIG_SYSTEM": os.devnull,
            **(env_overrides or {}),
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


def test_setup_puts_the_destination_on_the_installers_path(sandbox: Path) -> None:
    """install-mergiraf.sh refuses a destination that bare `mergiraf` does not
    resolve into. An adopter whose shell PATH lacks $HOME/.local/bin would hit
    that refusal on every run, so setup.sh prepends the destination for the
    installer call — and for that call only, since the driver it binds names an
    absolute path."""
    run_setup(sandbox, REGISTERS)

    on_path = (sandbox / "installer-path").read_text(encoding="utf-8").strip()
    bindir = str(sandbox / "home" / ".local" / "bin")
    assert on_path.split(os.pathsep)[0] == bindir
    assert bindir not in os.environ["PATH"].split(os.pathsep)


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


def _uv_lock_sandbox(sandbox: Path, *, with_uv: bool) -> dict:
    """Give the sandbox a uv.lock, and a PATH that does or does not carry uv.

    The stub records its argv, so the success case asserts `uv sync` actually ran
    rather than that setup.sh merely exited 0."""
    (sandbox / "uv.lock").write_text("", encoding="utf-8")
    bindir = sandbox / "stub-bin"
    bindir.mkdir(exist_ok=True)
    if with_uv:
        stub = bindir / "uv"
        stub.write_text(
            f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"{sandbox}/uv-calls"\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
    # A restricted PATH, so the real uv on this machine cannot answer for the
    # adopter machine that has none.
    return {"PATH": f"{bindir}:/usr/bin:/bin"}


def test_setup_syncs_the_python_environment_when_uv_is_present(sandbox: Path) -> None:
    env = _uv_lock_sandbox(sandbox, with_uv=True)

    result = run_setup(sandbox, REGISTERS, env)

    assert result.returncode == 0, result.stderr
    assert (sandbox / "uv-calls").read_text(encoding="utf-8").splitlines() == ["sync"]


def test_setup_fails_loud_when_uv_lock_has_no_uv(sandbox: Path) -> None:
    """A checkout with a uv.lock needs uv to realize it. Skipping the sync would
    report "Setup complete" over an unprovisioned interpreter, so the failure would
    surface later, inside a hook or a test that cannot explain it."""
    env = _uv_lock_sandbox(sandbox, with_uv=False)

    result = run_setup(sandbox, REGISTERS, env)

    assert result.returncode == 1
    assert "uv is not installed" in result.stderr
    assert "Setup complete" not in result.stdout
