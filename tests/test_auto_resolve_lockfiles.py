"""The lockfile fallback router: recognition, derivation, and the `--route` CLI.

Every derive/check runs a FAKE tool on PATH that records its own argv and cwd, so
these tests drive the real subprocess boundary (`regenerate`, `main`) rather than
asserting against source text.
"""

# covers: .github/resolver/auto-resolve/_lockfiles.py

import json
import os
import stat
import subprocess
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
    assert any(line.startswith("regenerated\tuv.lock\t") for line in lines)
    assert "deferred\tsub/Cargo.lock" in lines
    assert any(line.startswith("refused\tgo.sum\t") for line in lines)
    assert not any("README.md" in line for line in lines)
    assert log.exists()
    assert not cargo_log.exists()


def test_route_reads_a_padded_owned_directory_as_the_directory(
    tmp_path, fake_bin, monkeypatch
):
    """An `--owned` line carrying surrounding whitespace still names what it
    names. Read raw, ` vendor/ ` ends in a space rather than a slash, so it is
    filed as an exact path, and the lockfile under it falls through to this
    resolver's own rules — the precedence the caller's rule table holds."""
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "uv.lock").write_text("", encoding="utf-8")
    (vendor / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    log = tmp_path / "uv.log"
    _recording_tool(fake_bin, "uv", log)
    owned_file = tmp_path / "owned.txt"
    owned_file.write_text("  vendor/  \n", encoding="utf-8")

    result = _run_cli(
        [
            "--root",
            str(tmp_path),
            "--owned-file",
            str(owned_file),
            "--",
            "vendor/uv.lock",
        ]
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["caller-owned\tvendor/uv.lock"]
    assert not log.exists(), "the caller's own generator must be the one that runs"


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

    verdict = lockfiles._route_one("sub/uv.lock", str(root), lockfiles.EMPTY, set())

    assert verdict is not None
    assert "\n" not in verdict
    assert verdict.startswith("refused\tsub/uv.lock\t")
    assert verdict.count("\t") == 2


def test_regenerate_clears_a_lockfile_still_holding_conflict_markers(
    tmp_path, fake_bin
):
    """A lockfile routed here for a CLEAN manifest can still be marker-laden
    itself (a real conflict, not the auto-merged-clean shape). Every derive
    command reads the existing lockfile as a hint, so marker text must not
    reach it — regenerate() must succeed by clearing the file first."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n", encoding="utf-8"
    )
    log = tmp_path / "uv.log"
    _recording_tool(fake_bin, "uv", log, extra=f'echo relocked > "{tmp_path}/uv.lock"')

    touched = lockfiles.regenerate("uv.lock", str(tmp_path))

    assert touched == ["uv.lock"]
    assert (tmp_path / "uv.lock").read_text(encoding="utf-8") == "relocked\n"
    # Both the derive and the idempotence-check re-run saw a clean, non-marker
    # lockfile — never the conflicted bytes this test started with.
    assert "<<<<<<<" not in log.read_text(encoding="utf-8")


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_regenerate_reseeds_a_conflicted_lockfile_from_seed_ref(tmp_path, fake_bin):
    """A conflicted lockfile whose manifest is clean must be RESTORED from
    `seed_ref` (the merge base) before relocking, not deleted. An empty file
    gives the derive command no hint, so a real lock tool re-resolves every
    transitive dependency from nothing and drifts past what the manifest
    change actually forced — the defect glovebox PR #5150 hit twice."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("base-lock-contents\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (repo / "uv.lock").write_text(
        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n", encoding="utf-8"
    )
    seen_before_relock = tmp_path / "seen.txt"
    _write_fake_tool(
        fake_bin,
        "uv",
        f'if [ ! -f "{seen_before_relock}" ]; then '
        f'cat "{repo}/uv.lock" > "{seen_before_relock}"; fi\n'
        f'echo relocked > "{repo}/uv.lock"\n'
        "exit 0",
    )

    touched = lockfiles.regenerate("uv.lock", str(repo), seed_ref=base_sha)

    assert touched == ["uv.lock"]
    # The derive command's first (real) invocation must have seen the seeded
    # base bytes, never an empty file.
    assert seen_before_relock.read_text(encoding="utf-8") == "base-lock-contents\n"


def test_route_cli_threads_seed_ref_through_to_the_relock(
    tmp_path, fake_bin, monkeypatch
):
    """`--seed-ref` is prepare.sh's only way to ask for a seeded relock, so the
    test above passes `seed_ref=` to a function prepare.sh never calls directly.
    Drop the argument from the `_route_one` call, or mistype the `add_argument`
    name, and every lockfile silently relocks unseeded with the suite green."""
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("base-lock-contents\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (repo / "uv.lock").write_text(
        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n", encoding="utf-8"
    )
    seen = tmp_path / "cli-seen.txt"
    _write_fake_tool(
        fake_bin,
        "uv",
        f'if [ ! -f "{seen}" ]; then cat "{repo}/uv.lock" > "{seen}"; fi\n'
        f'echo relocked > "{repo}/uv.lock"\n'
        "exit 0",
    )

    result = _run_cli(["--root", str(repo), "--seed-ref", base_sha, "--", "uv.lock"])

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["regenerated\tuv.lock\tuv.lock"]
    assert seen.read_text(encoding="utf-8") == "base-lock-contents\n"


def test_a_seed_ref_that_cannot_be_read_says_so_on_stderr(tmp_path, fake_bin):
    """The fallback to an unseeded relock reinstates the drift this seeding
    removes, and a bad ref reads exactly like the one benign cause (a lockfile
    new since the merge base). Silence would leave the operator a `Regenerated`
    line that cannot tell a minimal relock from a drifted one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "uv.lock").write_text(
        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n", encoding="utf-8"
    )
    _write_fake_tool(fake_bin, "uv", f'echo relocked > "{repo}/uv.lock"\nexit 0')

    result = _run_cli(
        ["--root", str(repo), "--seed-ref", "no-such-ref", "--", "uv.lock"]
    )

    assert result.returncode == 0, result.stderr
    assert "no seed at no-such-ref" in result.stderr
    assert "re-resolves every transitive dependency" in result.stderr


def test_a_marker_free_unmerged_lockfile_is_still_reseeded(tmp_path, fake_bin):
    """`uv.lock` carries `-merge` in the repositories this resolver runs against,
    so git never writes markers into it: the merge leaves OUR whole lockfile on
    disk and the conflict lives only in the index. A marker test alone reads that
    as clean, seeds the derive command from one parent's lockfile, and pins that
    parent's whole transitive set — the exact drift the seeding removes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / ".gitattributes").write_text("uv.lock -merge\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (repo / "uv.lock").write_text("base-lock-contents\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "base", cwd=repo)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    _git("checkout", "-q", "-b", "side", cwd=repo)
    (repo / "uv.lock").write_text("their-lock-contents\n", encoding="utf-8")
    _git("commit", "-q", "-am", "side", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)
    (repo / "uv.lock").write_text("our-lock-contents\n", encoding="utf-8")
    _git("commit", "-q", "-am", "ours", cwd=repo)
    merge = subprocess.run(
        ["git", "merge", "side"], cwd=repo, capture_output=True, text=True
    )
    assert merge.returncode != 0, merge.stdout + merge.stderr
    # The premise, asserted rather than assumed: git left no markers, so only the
    # index says this path is conflicted.
    assert (repo / "uv.lock").read_text(encoding="utf-8") == "our-lock-contents\n"

    seen = tmp_path / "unmerged-seen.txt"
    _write_fake_tool(
        fake_bin,
        "uv",
        f'if [ ! -f "{seen}" ]; then cat "{repo}/uv.lock" > "{seen}"; fi\n'
        f'echo relocked > "{repo}/uv.lock"\n'
        "exit 0",
    )

    touched = lockfiles.regenerate("uv.lock", str(repo), seed_ref=base_sha)

    assert touched == ["uv.lock"]
    assert seen.read_text(encoding="utf-8") == "base-lock-contents\n"


def test_regenerate_stages_a_declared_co_output(tmp_path, fake_bin):
    """`go.sum`'s generator legitimately rewrites `go.mod` too; the caller
    must be told about both paths, or the pair splits across commits."""
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    (tmp_path / "go.sum").write_text("", encoding="utf-8")
    log = tmp_path / "go.log"
    _recording_tool(
        fake_bin,
        "go",
        log,
        extra=(
            f'if [ "$1" = mod ] && [ "$2" = tidy ]; then '
            f'echo tidied >> "{tmp_path}/go.mod"; fi'
        ),
    )

    touched = lockfiles.regenerate("go.sum", str(tmp_path))

    assert sorted(touched) == ["go.mod", "go.sum"]


def test_idempotence_failure_restores_the_first_runs_bytes(tmp_path, fake_bin):
    """A rule with no dedicated `check` command proves trust by re-running the
    derive and comparing. On a mismatch, the second run's bytes must not be
    left on disk for a later `git add -A` to stage."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("first\n", encoding="utf-8")
    counter = tmp_path / "npm-calls"
    _write_fake_tool(
        fake_bin,
        "npm",
        f'n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n + 1)); '
        f'echo "$n" > "{counter}"; '
        f'echo "run-$n" > "{tmp_path}/package-lock.json"; exit 0',
    )

    with pytest.raises(lockfiles.LockfileError, match="not idempotent"):
        lockfiles.regenerate("package-lock.json", str(tmp_path))

    assert (tmp_path / "package-lock.json").read_text(encoding="utf-8") == "run-1\n"
