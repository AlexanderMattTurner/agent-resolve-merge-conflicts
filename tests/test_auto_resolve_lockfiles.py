"""The lockfile fallback router: recognition, derivation, and the `--route` CLI.

Every derive/check runs a FAKE tool on PATH that records its own argv and cwd, so
these tests drive the real subprocess boundary (`regenerate`, `main`) rather than
asserting against source text.
"""

# covers: .github/resolver/auto-resolve/_lockfiles.py

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from tests._resolver_helpers import REPO_ROOT, load_script

lockfiles = load_script(".github/resolver/auto-resolve/_lockfiles.py")

_SCRIPT_PATH = REPO_ROOT / ".github" / "resolver" / "auto-resolve" / "_lockfiles.py"

ALL_BASENAMES = [
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
]


def _write_fake_tool(bin_dir: Path, name: str, body: str) -> Path:
    """A fake `name` on PATH that runs `body` (a shell script fragment) and
    records its own argv/cwd. `body` writes whatever this test needs before or
    instead of exiting 0."""
    script = bin_dir / name
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _recording_tool(bin_dir: Path, name: str, log: Path, extra: str = "") -> None:
    """A fake tool that appends `argv<TAB>cwd` to `log` and exits 0."""
    _write_fake_tool(
        bin_dir,
        name,
        f'printf "%s\\t%s\\n" "$*" "$(pwd)" >> "{log}"\n{extra}\nexit 0',
    )


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


# --- 1. Recognition, member by member -------------------------------------


@pytest.mark.parametrize("basename", ALL_BASENAMES)
def test_rule_for_recognizes_each_lockfile(basename):
    assert lockfiles.rule_for(basename) is not None


def test_rule_for_rejects_unrelated_path():
    assert lockfiles.rule_for("README.md") is None


@pytest.mark.parametrize("basename", ALL_BASENAMES)
def test_rule_for_recognizes_nested_path(basename):
    assert lockfiles.rule_for(f"sub/dir/{basename}") is not None


# --- 2. derivable() ---------------------------------------------------------


def test_derivable_false_without_manifest(tmp_path):
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    assert lockfiles.derivable("uv.lock", str(tmp_path)) is False


def test_derivable_true_with_manifest(tmp_path):
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert lockfiles.derivable("uv.lock", str(tmp_path)) is True


def test_derivable_go_sum(tmp_path):
    (tmp_path / "go.sum").write_text("", encoding="utf-8")
    assert lockfiles.derivable("go.sum", str(tmp_path)) is False
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    assert lockfiles.derivable("go.sum", str(tmp_path)) is True


# --- 3. regenerate() runs derive then check, from the lockfile's directory --


def test_regenerate_runs_derive_then_check_from_lockfile_dir(tmp_path, fake_bin):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "uv.lock").write_text("", encoding="utf-8")
    (sub / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    log = tmp_path / "uv.log"
    _recording_tool(fake_bin, "uv", log)

    lockfiles.regenerate("sub/uv.lock", str(tmp_path))

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    argv0, cwd0 = lines[0].split("\t")
    argv1, cwd1 = lines[1].split("\t")
    assert argv0 == "lock"
    assert argv1 == "lock --check"
    assert cwd0 == cwd1 == str(sub.resolve())


# --- 4. poetry version gating -----------------------------------------------


@pytest.mark.parametrize(
    "version_output,expected",
    [
        ("Poetry (version 1.8.3)\n", "lock --no-update"),
        ("Poetry (version 2.1.0)\n", "lock"),
    ],
)
def test_poetry_version_gates_no_update_flag(
    tmp_path, fake_bin, version_output, expected
):
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.poetry]\nname='x'\n", encoding="utf-8"
    )
    log = tmp_path / "poetry.log"
    _write_fake_tool(
        fake_bin,
        "poetry",
        f"""
if [ "$1" = "--version" ]; then
  printf '{version_output}'
  exit 0
fi
printf "%s\\t%s\\n" "$*" "$(pwd)" >> "{log}"
exit 0
""",
    )

    lockfiles.regenerate("poetry.lock", str(tmp_path))

    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].split("\t")[0] == expected


# --- 5. yarn berry vs classic ------------------------------------------------


def test_yarn_berry_uses_update_lockfile_mode(tmp_path, fake_bin):
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"packageManager": "yarn@4.1.0"}), encoding="utf-8"
    )
    log = tmp_path / "yarn.log"
    _recording_tool(fake_bin, "yarn", log)

    lockfiles.regenerate("yarn.lock", str(tmp_path))

    assert log.read_text(encoding="utf-8").splitlines()[0].split("\t")[0] == (
        "install --mode=update-lockfile"
    )


def test_yarn_classic_uses_ignore_scripts(tmp_path, fake_bin):
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({}), encoding="utf-8")
    log = tmp_path / "yarn.log"
    _recording_tool(fake_bin, "yarn", log)

    lockfiles.regenerate("yarn.lock", str(tmp_path))

    assert log.read_text(encoding="utf-8").splitlines()[0].split("\t")[0] == (
        "install --ignore-scripts"
    )


# --- 6. scrubbed_env ---------------------------------------------------------


def test_scrubbed_env_drops_secrets_and_runner_channels(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "shh")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "authorization: x")
    monkeypatch.setenv("GITHUB_ENV", "/tmp/env")
    monkeypatch.setenv("ORDINARY_VAR", "keep-me")

    env = lockfiles.scrubbed_env()

    assert "GITHUB_TOKEN" not in env
    assert "GIT_CONFIG_VALUE_0" not in env
    assert "GITHUB_ENV" not in env
    assert env["ORDINARY_VAR"] == "keep-me"


# --- 7. LockfileError cases ---------------------------------------------------


def test_missing_tool_raises_naming_binary(tmp_path, fake_bin):
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    with pytest.raises(lockfiles.LockfileError, match="uv"):
        lockfiles.regenerate("uv.lock", str(tmp_path))


def test_failing_derive_raises_naming_command(tmp_path, fake_bin):
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    _write_fake_tool(fake_bin, "uv", "exit 1")

    with pytest.raises(lockfiles.LockfileError, match="uv lock"):
        lockfiles.regenerate("uv.lock", str(tmp_path))


def test_failing_check_raises(tmp_path, fake_bin):
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    _write_fake_tool(
        fake_bin,
        "uv",
        'if [ "$1" = "lock" ] && [ "$2" = "--check" ]; then exit 1; fi\nexit 0',
    )

    with pytest.raises(lockfiles.LockfileError, match="lock --check"):
        lockfiles.regenerate("uv.lock", str(tmp_path))


# --- 8. Idempotence check for rules with no dedicated `check` ---------------


def test_idempotence_check_raises_on_unstable_output(tmp_path, fake_bin):
    (tmp_path / "package-lock.json").write_text("orig", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({}), encoding="utf-8")
    _write_fake_tool(
        fake_bin,
        "npm",
        'date +%s%N > "package-lock.json"\nexit 0',
    )

    with pytest.raises(lockfiles.LockfileError, match="not idempotent"):
        lockfiles.regenerate("package-lock.json", str(tmp_path))


def test_idempotence_check_passes_on_stable_output(tmp_path, fake_bin):
    (tmp_path / "package-lock.json").write_text("orig", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({}), encoding="utf-8")
    _write_fake_tool(
        fake_bin,
        "npm",
        'printf "stable" > "package-lock.json"\nexit 0',
    )

    lockfiles.regenerate("package-lock.json", str(tmp_path))

    assert (tmp_path / "package-lock.json").read_text(encoding="utf-8") == "stable"


# --- 9. --route CLI ----------------------------------------------------------


def _run_cli(args: list[str]):
    import subprocess

    return subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--route", *args],
        capture_output=True,
        text=True,
        env=os.environ,
    )


def test_route_cli(tmp_path, fake_bin, monkeypatch):
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    # caller-owned path
    (tmp_path / "owned.lock").write_text("", encoding="utf-8")
    owned_file = tmp_path / "owned.txt"
    owned_file.write_text("owned.lock\n", encoding="utf-8")

    # derivable, clean manifest -> regenerated
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    log = tmp_path / "uv.log"
    _recording_tool(fake_bin, "uv", log)

    # derivable, but manifest is conflicted -> deferred, tool must not run
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "Cargo.lock").write_text("", encoding="utf-8")
    (sub / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    cargo_log = tmp_path / "cargo.log"
    _recording_tool(fake_bin, "cargo", cargo_log)

    # recognized, no manifest -> refused
    (tmp_path / "go.sum").write_text("", encoding="utf-8")

    # unrecognized -> nothing printed
    (tmp_path / "README.md").write_text("", encoding="utf-8")

    result = _run_cli(
        [
            "--root",
            str(tmp_path),
            "--owned-file",
            str(owned_file),
            "--manifest-conflicted",
            "sub/Cargo.toml",
            "--",
            "owned.lock",
            "uv.lock",
            "sub/Cargo.lock",
            "go.sum",
            "README.md",
        ]
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "caller-owned\towned.lock" in lines
    assert "regenerated\tuv.lock" in lines
    assert "deferred\tsub/Cargo.lock" in lines
    assert any(line.startswith("refused\tgo.sum\t") for line in lines)
    assert not any("README.md" in line for line in lines)
    assert log.exists()
    assert not cargo_log.exists()


def test_route_keeps_a_failing_tools_output_on_one_line(tmp_path, monkeypatch):
    """A verdict line is TAB-separated and newline-terminated, so a failing
    tool's multi-line output inside a reason would be read by the bash caller as
    further verdicts — one of them for an empty path."""
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (root / "sub" / "uv.lock").write_text("lock\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "uv"
    fake.write_text(
        "#!/usr/bin/env bash\nprintf 'warning: line one\\nwarning: line two\\n' >&2\nexit 2\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    verdict = lockfiles._route_one("sub/uv.lock", str(root), set(), set())

    assert verdict is not None
    assert "\n" not in verdict
    assert verdict.startswith("refused\tsub/uv.lock\t")
    assert verdict.count("\t") == 2
