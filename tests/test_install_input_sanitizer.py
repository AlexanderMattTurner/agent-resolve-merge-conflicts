"""The sanitizer installs beside the script that imports it, not beside the cwd.

covers: .github/scripts/install-input-sanitizer.sh

merge-delta-review.yaml runs this script from a CLONE while the working
directory is the calling repository's own checkout. A cwd-relative prefix put
the package in the caller's tree, where the importer — running from the clone —
never resolves it. Nothing failed loudly: the sanitizer is reached through an
ESM import that only breaks at review time, in the consumer's job.

`npm` is stubbed because the real one would reach the registry, which the rule
against stubbing a tool exempts as a network dependency. Nothing about its REPLY
is invented: the assertion is on the argv the real script produced.
"""

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
SCRIPT = REPO_ROOT / ".github/scripts/install-input-sanitizer.sh"


def _prefix_from(cwd: Path, tmp_path: Path) -> Path:
    """Run the installer with a recording `npm`, from `cwd`.

    Answers the `--prefix` it passed, resolved AGAINST THAT CWD — which is the
    whole question. Resolving it in this process instead would silently answer
    for the test runner's directory, and a cwd-relative prefix would then look
    identical to a script-relative one.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    capture = tmp_path / "argv"
    stub = bindir / "npm"
    stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >>"{capture}"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=cwd,
        check=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
    )
    argv = capture.read_text(encoding="utf-8").split("\n")
    assert "--prefix" in argv, argv
    return Path(cwd, argv[argv.index("--prefix") + 1]).resolve()


def test_the_prefix_is_the_script_s_own_directory(tmp_path: Path) -> None:
    """From an unrelated working directory, the install still lands beside the
    importer. A cwd-relative prefix would name a `.github/scripts` under the
    scratch tree, which is where the consumer's copy would have been."""
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()

    assert _prefix_from(elsewhere, tmp_path) == SCRIPT.parent.resolve()


def test_the_prefix_does_not_move_with_the_working_directory(tmp_path: Path) -> None:
    """The same answer from the repository root and from an unrelated tree. The
    old cwd-relative form gave two different answers, which is the whole defect."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    assert _prefix_from(REPO_ROOT, tmp_path / "a") == _prefix_from(
        elsewhere, tmp_path / "b"
    )
