"""`resolver-dir.sh` picks the resolver tree each job renders with.

covers: .github/scripts/resolver-dir.sh

Two rules, and each has a cost when it fires wrongly. In the resolver's own
repository the checkout IS the resolver at the sha under test, so a clone there
would render a renderer PR's own merge deltas with the previous release. Anywhere
else the synced `.github/resolver` tracks the template's default branch, so
reading it in place would run upstream code the consumer never accepted.
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
)

_SCRIPT = REPO_ROOT / ".github" / "scripts" / "resolver-dir.sh"
_RESOLVER = "AlexanderMattTurner/agent-resolve-merge-conflicts"
_RENDERER = ".github/resolver/remerge-diff-report.py"


def _run(cwd: Path, tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    out = tmp_path / "github_output"
    out.touch()
    environ = {
        **os.environ,
        "GITHUB_OUTPUT": str(out),
        "RUNNER_TEMP": str(tmp_path / "runner_temp"),
        "RESOLVER_REPOSITORY": _RESOLVER,
        **env,
    }
    (tmp_path / "runner_temp").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(_SCRIPT)], cwd=cwd, env=environ, capture_output=True, text=True
    )


def _emitted_dir(done: subprocess.CompletedProcess, tmp_path: Path) -> str:
    assert done.returncode == 0, done.stderr
    line = (tmp_path / "github_output").read_text(encoding="utf-8").strip()
    assert line.startswith("dir="), line
    return line[len("dir=") :]


def _write_renderer(root: Path, body: str) -> None:
    path = root / _RENDERER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _bare_remote(tmp_path: Path) -> tuple[Path, str]:
    """A local repository standing in for the resolver, with two renderers.

    Returns the bare path and the sha of the OLDER commit, so a test can prove
    the clone checked out the pin rather than the remote's HEAD.
    """
    work = tmp_path / "upstream"
    work.mkdir()
    git = ["git", "-C", str(work)]
    subprocess.run([*git[:1], "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run([*git, "config", "user.email", "t@t"], check=True)
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    _write_renderer(work, "PINNED\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "pinned"], check=True)
    pinned = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, check=True, text=True
    ).stdout.strip()
    _write_renderer(work, "HEAD\n")
    subprocess.run([*git, "commit", "-qam", "head"], check=True)

    bare = tmp_path / "upstream.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare, pinned


def _consumer(tmp_path: Path, bare: Path, ref: str) -> tuple[Path, dict[str, str]]:
    """A checkout that is NOT the resolver, with `https://github.com/` redirected.

    The redirect keeps the test off the network without giving the script a knob
    production does not have.
    """
    consumer = tmp_path / "consumer"
    (consumer / ".github" / "workflows").mkdir(parents=True)
    (consumer / ".github" / "workflows" / "auto-resolve-conflicts.yaml").write_text(
        f"jobs:\n  resolve:\n    uses: owner/repo/.github/workflows/auto-resolve.yaml@{ref}\n",
        encoding="utf-8",
    )
    scripts = consumer / ".github" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "resolver-ref.py").write_bytes(
        (REPO_ROOT / ".github/scripts/resolver-ref.py").read_bytes()
    )

    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        f'[url "{bare}"]\n\tinsteadOf = https://github.com/{_RESOLVER}.git\n',
        encoding="utf-8",
    )
    return consumer, {"HOME": str(home), "GITHUB_REPOSITORY": "someone/consumer"}


def test_the_resolver_s_own_repository_reads_the_renderer_in_place(tmp_path: Path):
    """A clone here would render a renderer PR's deltas with the last release."""
    checkout = tmp_path / "checkout"
    _write_renderer(checkout, "IN TREE\n")
    done = _run(checkout, tmp_path, GITHUB_REPOSITORY=_RESOLVER)
    assert _emitted_dir(done, tmp_path) == str(checkout / ".github" / "resolver")


def test_the_repository_match_ignores_case(tmp_path: Path):
    """The two spellings reach the script from a literal and the event context."""
    checkout = tmp_path / "checkout"
    _write_renderer(checkout, "IN TREE\n")
    done = _run(checkout, tmp_path, GITHUB_REPOSITORY=_RESOLVER.lower())
    assert _emitted_dir(done, tmp_path) == str(checkout / ".github" / "resolver")


def test_a_sparse_checkout_missing_the_renderer_refuses(tmp_path: Path):
    """This is the failure that went red as a bare FileNotFoundError before."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    done = _run(checkout, tmp_path, GITHUB_REPOSITORY=_RESOLVER)
    assert done.returncode != 0, done.stdout
    assert "sparse-checkout" in done.stderr
    assert (tmp_path / "github_output").read_text(encoding="utf-8") == ""


def test_a_consumer_gets_the_pinned_sha_and_never_the_remote_head(tmp_path: Path):
    """The pin IS the security control: HEAD is code the consumer never accepted."""
    bare, pinned = _bare_remote(tmp_path)
    consumer, env = _consumer(tmp_path, bare, pinned)
    done = _run(consumer, tmp_path, **env)
    served = Path(_emitted_dir(done, tmp_path)) / "remerge-diff-report.py"
    assert served.read_text(encoding="utf-8") == "PINNED\n"


def test_a_pinned_sha_carrying_no_renderer_refuses(tmp_path: Path):
    """Fail loud, rather than hand a caller a path with nothing behind it."""
    bare, _ = _bare_remote(tmp_path)
    strip = tmp_path / "strip"
    subprocess.run(["git", "clone", "-q", str(bare), str(strip)], check=True)
    git = ["git", "-C", str(strip)]
    subprocess.run([*git, "config", "user.email", "t@t"], check=True)
    subprocess.run([*git, "config", "user.name", "t"], check=True)
    subprocess.run([*git, "rm", "-q", _RENDERER], check=True)
    subprocess.run([*git, "commit", "-qm", "no renderer"], check=True)
    subprocess.run([*git, "push", "-q", "origin", "HEAD:stripped"], check=True)
    stripped = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, check=True, text=True
    ).stdout.strip()

    consumer, env = _consumer(tmp_path, bare, stripped)
    done = _run(consumer, tmp_path, **env)
    assert done.returncode != 0, done.stdout
    assert "carries no" in done.stderr


def test_each_required_variable_fails_loudly(tmp_path: Path):
    """An unset variable must never fall back to a path with nothing behind it."""
    checkout = tmp_path / "checkout"
    _write_renderer(checkout, "IN TREE\n")
    for missing in (
        "GITHUB_OUTPUT",
        "GITHUB_REPOSITORY",
        "RUNNER_TEMP",
        "RESOLVER_REPOSITORY",
    ):
        env = {
            **os.environ,
            "GITHUB_OUTPUT": str(tmp_path / "github_output"),
            "GITHUB_REPOSITORY": _RESOLVER,
            "RUNNER_TEMP": str(tmp_path),
            "RESOLVER_REPOSITORY": _RESOLVER,
        }
        env.pop(missing)
        done = subprocess.run(
            ["bash", str(_SCRIPT)],
            cwd=checkout,
            env=env,
            capture_output=True,
            text=True,
        )
        assert done.returncode != 0, missing
        assert missing in done.stderr
