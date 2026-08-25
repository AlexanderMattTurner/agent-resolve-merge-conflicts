"""`release-tag.sh` drives the live release, so drive it against a real remote.

PROBLEM CLASS — machinery that only ever ran in dry-run has an untested half.
Every dry run of this script exits before the first git write, so the changelog
commit, the atomic push, the major-tag move and the major-tag repair had never
executed anywhere when the flow went live. A defect in any of them lands on the
default branch of a repository nobody is watching.

Each test builds a bare remote and a clone, runs the real script under bash, and
reads the outcome back off the REMOTE — what a caller pinning a tag would see.
"""

import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT, commit_all, git_env, init_test_repo

SCRIPT = REPO_ROOT / ".github" / "scripts" / "release-tag.sh"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, env=git_env(), capture_output=True, text=True
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout.strip()


@pytest.fixture(name="clone")
def _clone(tmp_path: Path) -> Path:
    """A clone of a bare remote, seeded with the CHANGELOG the script promotes."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", remote], check=True)
    repo = tmp_path / "work"
    init_test_repo(repo)
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n### Added\n\n- a curated note\n",
        encoding="utf-8",
    )
    # The caller the release advances. Every live release rewrites this line to
    # the release commit, so the fixture carries it for the same reason the real
    # repository does.
    caller = repo / ".github" / "workflows" / "auto-resolve-conflicts.yaml"
    caller.parent.mkdir(parents=True, exist_ok=True)
    caller.write_text(
        "jobs:\n  resolve:\n    uses: own/repo/.github/workflows/auto-resolve.yaml@"
        + "0" * 40
        + " # v0.9.0\n",
        encoding="utf-8",
    )
    # The script copies .github/scripts/promote-changelog.mjs from beside itself,
    # so the sandbox needs no copy of it.
    commit_all(repo, "feat(seed): the first commit")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _run_release(
    repo: Path, live: bool = True, output: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the real script against the fixture's remote, whatever its outcome."""
    env = {
        **git_env(),
        "RELEASE_DRY_RUN": "false" if live else "true",
        "RELEASE_MAJOR_TAG": "v1",
        "GITHUB_REPOSITORY": "own/repo",
    }
    if output is not None:
        env["GITHUB_OUTPUT"] = str(output)
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=repo, env=env, capture_output=True, text=True
    )


def _release(repo: Path, live: bool = True) -> dict[str, str]:
    """Run the script; return its `$GITHUB_OUTPUT` as a mapping."""
    out = repo.parent / "step-output"
    out.write_text("", encoding="utf-8")
    done = _run_release(repo, live=live, output=out)
    assert done.returncode == 0, done.stderr
    return dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines()
    )


def test_the_first_release_lands_on_the_remote_as_v1_0_0(clone: Path) -> None:
    """The whole live path: version tag, moving major tag and the changelog
    commit all reach the remote, and `released` claims it only afterwards."""
    result = _release(clone)
    assert result["version"] == "1.0.0"
    assert result["released"] == "true"
    remote = Path(_git(clone, "remote", "get-url", "origin"))
    assert _git(remote, "rev-list", "-1", "v1.0.0") == _git(
        remote, "rev-list", "-1", "v1"
    )
    # HEAD~1: the pin advance is the commit that follows the release.
    assert _git(remote, "rev-list", "-1", "v1.0.0") == _git(
        clone, "rev-parse", "HEAD~1"
    )
    assert _git(remote, "log", "-1", "--format=%s", "main~1").startswith(
        "chore(release): v1.0.0"
    )
    changelog = _git(remote, "show", "main:CHANGELOG.md")
    assert "## [1.0.0]" in changelog and "## Unreleased" in changelog
    # The curated notes are the release notes. Promoting the commit subjects
    # over them deletes every hand-written entry on the first release.
    assert changelog.index("## [1.0.0]") < changelog.index("- a curated note")


def test_a_dry_run_decides_a_version_and_writes_nothing(clone: Path) -> None:
    """The parked mode still reports what it would cut, so a human can read a
    live cycle's version before turning the flow on."""
    result = _release(clone, live=False)
    assert result["version"] == "1.0.0"
    assert result["released"] == "false"
    assert _git(clone, "tag") == ""
    remote = Path(_git(clone, "remote", "get-url", "origin"))
    assert _git(remote, "tag") == ""


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("feat(x): add a thing", "1.1.0"),
        ("fix(x): repair a thing", "1.0.1"),
        ("feat(x)!: break a thing", "1.1.0"),
        ("fix(x)!: break a thing", "1.1.0"),
        ("fix(x): repair a thing\n\nBREAKING CHANGE: the flag is gone", "1.1.0"),
    ],
)
def test_the_bump_comes_from_the_commits_since_the_last_tag(
    clone: Path, subject: str, expected: str
) -> None:
    """A breaking marker caps at minor: callers here read `v1`, so an automated
    major would strand every one of them behind a tag that stops moving."""
    _release(clone)
    commit_all(clone, subject)
    _git(clone, "push", "-q", "origin", "main")
    assert _release(clone)["version"] == expected


def test_a_major_tag_left_behind_is_repaired_without_cutting_a_version(
    clone: Path,
) -> None:
    """The version tag pushes before the major tag, so the second push can fail
    on its own. Without the repair every later run takes the already-tagged exit
    and `v1` stays stale until a human notices."""
    _release(clone)
    remote = Path(_git(clone, "remote", "get-url", "origin"))
    released = _git(remote, "rev-list", "-1", "v1")
    _git(clone, "push", "-q", "--delete", "origin", "v1")
    _git(clone, "tag", "-d", "v1")

    result = _release(clone)

    assert "version" not in result, "cut a second version for the same commit"
    assert _git(remote, "rev-list", "-1", "v1") == released


def _pinned(repo: Path, ref: str = "origin/main") -> str:
    """The caller's pin as the REMOTE holds it — what a run would actually use."""
    line = _git(repo, "show", f"{ref}:.github/workflows/auto-resolve-conflicts.yaml")
    return line.split("auto-resolve.yaml@", 1)[1].splitlines()[0].strip()


def test_the_release_advances_the_caller_pin_to_the_release_commit(
    clone: Path,
) -> None:
    """A pin left on the previous release runs the previous release's resolver:
    the called workflow reads its scripts from the pinned commit, so merged code
    is inert in production and nothing reports it. The pin cannot name its own
    commit, so it lands in the commit after the release."""
    outputs = _release(clone)
    _git(clone, "fetch", "-q", "origin")
    released = _git(clone, "rev-list", "-1", f"v{outputs['version']}")
    assert _pinned(clone) == f"{released} # v{outputs['version']}"
    # The advance is its own commit, and it carries the release-docs subject, so
    # the next run does not cut a version whose only content is this pin.
    assert _git(clone, "log", "-1", "--pretty=%s", "origin/main").startswith(
        "chore(release): pin the caller"
    )
    assert "version" not in _release(clone), "cut a version for the pin commit"


def _seed_readme(repo: Path, pins: int) -> None:
    """A README documenting THIS resolver, with `pins` copies of the `uses:` line."""
    line = "uses: own/repo/.github/workflows/auto-resolve.yaml@{} # v0.9.0\n".format(
        "0" * 40
    )
    (repo / "README.md").write_text("# own/repo\n\n" + line * pins, encoding="utf-8")
    commit_all(repo, "docs(seed): document the pin")
    _git(repo, "push", "-q", "origin", "main")


def test_the_release_advances_both_readme_pins(clone: Path) -> None:
    """A reader copies the README's line, so a stale one hands them the previous
    release. The README states the pin twice — once to call the workflow and once
    to explain the form — and a rewrite that reached only the first would leave
    the other naming a commit no release points at."""
    _seed_readme(clone, 2)
    outputs = _release(clone)
    _git(clone, "fetch", "-q", "origin")
    released = _git(clone, "rev-list", "-1", f"v{outputs['version']}")
    readme = _git(clone, "show", "origin/main:README.md")
    pinned = [
        line.split("auto-resolve.yaml@", 1)[1]
        for line in readme.splitlines()
        if "auto-resolve.yaml@" in line
    ]
    assert pinned == [f"{released} # v{outputs['version']}"] * 2


def test_a_readme_that_lost_a_pin_fails_before_the_release_is_published(
    clone: Path,
) -> None:
    """The count is what makes the rewrite audible. A README reformatted down to
    one copy would otherwise take the rewrite silently, and the copy that moved
    out of the pattern's reach would keep naming an older release.

    The refusal has to land BEFORE the tags do. Refusing afterwards publishes a
    release whose caller still runs the previous one, and no re-run repairs it:
    the tag then names HEAD, so the next run cuts no version at all."""
    _seed_readme(clone, 1)
    done = _run_release(clone)
    assert done.returncode != 0
    assert "expected 2 resolver pin(s) in README.md" in done.stderr
    remote = Path(_git(clone, "remote", "get-url", "origin"))
    assert _git(remote, "tag") == "", "published a release the pin check refused"
    assert _git(remote, "log", "-1", "--pretty=%s", "main").startswith("docs(seed)")


def test_a_pin_commit_that_never_landed_is_finished_by_the_next_run(
    clone: Path,
) -> None:
    """The pin commit is pushed on its own, so its push can be rejected while the
    release stands. The next run reaches HEAD-is-already-tagged and would report
    nothing to do, leaving the caller on the previous release forever."""
    _seed_readme(clone, 2)
    outputs = _release(clone)
    released = _git(clone, "rev-list", "-1", f"v{outputs['version']}")
    # The state a rejected pin push leaves: the release is on the remote, the
    # commit that moves the pins is not.
    _git(clone, "reset", "-q", "--hard", released)
    _git(clone, "push", "-q", "--force", "origin", "HEAD:main")

    result = _release(clone)

    assert "version" not in result, "cut a second version for the same commit"
    _git(clone, "fetch", "-q", "origin")
    assert _pinned(clone) == f"{released} # v{outputs['version']}"


def test_a_caller_pinning_another_repositorys_resolver_is_left_alone(
    clone: Path,
) -> None:
    """This script is synced to every consumer, and a consumer's caller pins the
    RESOLVER's releases, not its own. Rewriting that line would point it at a
    commit the resolver repository has never seen."""
    caller = clone / ".github" / "workflows" / "auto-resolve-conflicts.yaml"
    before = "jobs:\n  resolve:\n    uses: other/resolver/.github/workflows/auto-resolve.yaml@{}\n".format(
        "0" * 40
    )
    caller.write_text(before, encoding="utf-8")
    commit_all(clone, "fix(seed): pin another repository's resolver")
    _git(clone, "push", "-q", "origin", "main")
    assert _release(clone)["released"] == "true"
    _git(clone, "fetch", "-q", "origin")
    assert _git(
        clone, "show", "origin/main:.github/workflows/auto-resolve-conflicts.yaml"
    ) == before.rstrip("\n")


def test_a_line_that_only_looks_like_the_pin_is_not_counted_as_one(
    clone: Path,
) -> None:
    """The reference reaches grep and sed as a REGEX, and it carries dots. Left
    unescaped they match any character, so a path that is not this resolver's
    counts as a pin: the count then reports a file it cannot rewrite correctly,
    and the rewrite would put a release sha on a line nothing can call."""
    caller = clone / ".github" / "workflows" / "auto-resolve-conflicts.yaml"
    decoy = "uses: own/repo/xgithub/workflows/auto-resolveXyaml@{}\n".format("0" * 40)
    caller.write_text(caller.read_text(encoding="utf-8") + decoy, encoding="utf-8")
    commit_all(clone, "fix(seed): add a path the wildcard would match")
    _git(clone, "push", "-q", "origin", "main")

    outputs = _release(clone)

    _git(clone, "fetch", "-q", "origin")
    released = _git(clone, "rev-list", "-1", f"v{outputs['version']}")
    assert _pinned(clone) == f"{released} # v{outputs['version']}"
    assert decoy.strip() in _git(
        clone, "show", "origin/main:.github/workflows/auto-resolve-conflicts.yaml"
    )


def test_a_caller_with_no_resolver_pin_fails_the_release_loudly(clone: Path) -> None:
    """Silence here is the whole defect class: a rename or a reformat that this
    rewrite no longer matches would leave the pin frozen and report success."""
    caller = clone / ".github" / "workflows" / "auto-resolve-conflicts.yaml"
    caller.write_text(
        "jobs:\n  resolve:\n    uses: own/repo/.github/workflows/auto-resolve.yaml@main\n",
        encoding="utf-8",
    )
    commit_all(clone, "fix(seed): drop the pin")
    _git(clone, "push", "-q", "origin", "main")
    done = _run_release(clone)
    assert done.returncode != 0
    assert "expected 1 resolver pin(s)" in done.stderr
