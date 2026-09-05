"""The mechanical rules that decide a modify/delete conflict without a model.

Each case builds a real scratch repository, drives an actual `git merge` to the conflict, and
asks `decide` what it answers. Every rule gets its firing case AND the case with its premise
removed, because a rule that always fired would pass the first alone.

# covers: .github/resolver/auto-resolve/_one_sided_verdict.py
"""

import subprocess
from pathlib import Path

import pytest

from tests._helpers import commit_files, git_env, init_test_repo
from tests._resolver_helpers import load_script

verdict = load_script(".github/resolver/auto-resolve/_one_sided_verdict.py")

_MODULE = "scripts/check-closure-python.py"
_MODULE_TEST = "tests/test_check_closure_python.py"
_FRAGMENT = "changelog.d/5402-derivation.changed.md"
_ENTRY = "Derive the live check's image dependence from one source."


def _merge_to_conflict(repo: Path, branch: str) -> None:
    """Merge BRANCH and require that it conflicts, so no case asserts over a clean merge."""
    done = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", branch],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0, (
        f"the fixture merged cleanly, so there is no conflict to decide:\n{done.stdout}{done.stderr}"
    )


def _decide(repo: Path, paths: list[str]) -> dict:
    # Bound through the module under test: `load_script` gives the test its own copy of
    # `_git_io`, and binding that one leaves the copy `decide` actually calls unbound.
    verdict.bind_repo(repo)
    return verdict.decide(paths)


@pytest.fixture(name="dead_module_repo")
def _dead_module_repo(tmp_path: Path):
    """A merge where `main` retired a module and the other branch kept polishing it.

    Returns the repo and a callable that adds a caller to one side before the merge, so the
    firing case and its non-vacuity twin build the same history and differ only in that.
    """

    def build(*, caller_on: str | None = None) -> Path:
        repo = tmp_path / (caller_on or "none")
        init_test_repo(repo)
        commit_files(
            repo,
            {
                _MODULE: "def run():\n    return 1\n",
                _MODULE_TEST: "def test_run():\n    assert True\n",
                # A sibling something names, so `scripts/` is a directory reached by name
                # rather than one a glob consumes. Without it no name search there means
                # anything, and the rule declines.
                "scripts/select-checks.sh": "echo picking\n",
                ".github/workflows/live.yaml": "run: bash scripts/select-checks.sh\n",
            },
            "base",
        )
        subprocess.run(
            ["git", "checkout", "-q", "-b", "feature"],
            cwd=repo,
            env=git_env(),
            check=True,
        )
        # Both paths edited on the branch: a path only ONE side changed is a clean delete,
        # never a conflict, so an unedited test file would leave nothing here to decide.
        polished = {
            _MODULE: "def run():\n    return 2\n",
            _MODULE_TEST: "def test_run():\n    assert 1 == 1\n",
        }
        if caller_on == "survivor":
            polished["scripts/select-checks.sh"] = f"echo picking\npython3 {_MODULE}\n"
        commit_files(repo, polished, "polish the module")
        subprocess.run(
            ["git", "checkout", "-q", "main"], cwd=repo, env=git_env(), check=True
        )
        if caller_on == "deleter":
            commit_files(repo, {"docs/inventory.md": f"still lists {_MODULE}\n"}, "doc")
        subprocess.run(
            ["git", "rm", "-q", _MODULE, _MODULE_TEST],
            cwd=repo,
            env=git_env(),
            check=True,
        )
        commit_files(repo, {}, "drop dead scripts with no entry point")
        _merge_to_conflict(repo, "feature")
        return repo

    return build


def test_a_retired_module_nothing_references_is_decided_as_a_deletion(dead_module_repo):
    """Neither side names the module, so the branch's edits reach no caller on its own branch.

    Both paths decide together: the module and its test name each other, so a pass that did not
    exclude the set it is deciding would find each alive through the other.
    """
    repo = dead_module_repo()
    decided = _decide(repo, [_MODULE, _MODULE_TEST])
    assert sorted(decided) == sorted([_MODULE, _MODULE_TEST])
    assert decided[_MODULE].rule == "unreferenced-on-both-sides"
    # The second sweep's only firing case: `tests/` holds no sibling reached by name, so the
    # reference rule declines there and nothing else pins which rule took this path.
    assert decided[_MODULE_TEST].rule == "follows-a-deleted-subject"


def test_a_caller_on_the_surviving_side_leaves_the_module_for_a_human(dead_module_repo):
    """The premise removed: the branch still calls the module, so its edits reach a caller."""
    repo = dead_module_repo(caller_on="survivor")
    assert _decide(repo, [_MODULE, _MODULE_TEST]) == {}


def test_a_caller_on_the_deleting_side_leaves_the_module_for_a_human(dead_module_repo):
    """A reference the DELETING side still carries blocks the rule too.

    Checking the survivor alone would decide this one, and the deleting side names a module it
    is removing — which is a half-finished retirement, not a settled one.
    """
    repo = dead_module_repo(caller_on="deleter")
    assert _decide(repo, [_MODULE, _MODULE_TEST]) == {}


def _fragment_repo(tmp_path: Path, *, released: bool) -> Path:
    """A merge where `main` released a changelog fragment and the branch reworded it."""
    repo = tmp_path / ("released" if released else "unreleased")
    init_test_repo(repo)
    commit_files(
        repo,
        {_FRAGMENT: f"- {_ENTRY}\n", "CHANGELOG.md": "# Changelog\n"},
        "base",
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"], cwd=repo, env=git_env(), check=True
    )
    commit_files(
        repo, {_FRAGMENT: "- Derive it from one source, reworded.\n"}, "reword"
    )
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=repo, env=git_env(), check=True
    )
    body = f"# Changelog\n\n## [0.55.0]\n\n### Changed\n\n- {_ENTRY if released else 'Something else entirely here.'}\n"
    subprocess.run(["git", "rm", "-q", _FRAGMENT], cwd=repo, env=git_env(), check=True)
    commit_files(repo, {"CHANGELOG.md": body}, "release v0.55.0")
    _merge_to_conflict(repo, "feature")
    return repo


def test_a_fragment_the_deleting_side_already_shipped_is_decided_as_a_deletion(
    tmp_path,
):
    """The release folded this entry into CHANGELOG.md, so the reword edits a shipped record."""
    repo = _fragment_repo(tmp_path, released=True)
    decided = _decide(repo, [_FRAGMENT])
    assert decided[_FRAGMENT].rule == "released-changelog-fragment"


def test_a_fragment_the_changelog_never_carried_is_left_for_a_human(tmp_path):
    """The premise removed: the deleting side's CHANGELOG.md does not carry this entry.

    The fragment is still unreferenced by every other file, so this is also what stops the
    reference rule from deciding every changelog conflict on its own.
    """
    repo = _fragment_repo(tmp_path, released=False)
    assert _decide(repo, [_FRAGMENT]) == {}


def test_a_path_only_one_side_ever_had_is_never_decided(tmp_path):
    """An add/add-shaped one-sided path carries no deletion, so no rule here applies to it."""
    repo = tmp_path / "added"
    init_test_repo(repo)
    commit_files(repo, {"README.md": "a repository\n"}, "base")
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"], cwd=repo, env=git_env(), check=True
    )
    commit_files(repo, {"scripts/new.py": "x = 1\n"}, "add on the branch")
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=repo, env=git_env(), check=True
    )
    commit_files(repo, {"scripts/new.py": "x = 2\n"}, "add on main")
    _merge_to_conflict(repo, "feature")
    assert _decide(repo, ["scripts/new.py"]) == {}


def test_a_name_that_merely_contains_a_deleted_stem_is_left_alone(tmp_path):
    """`scorecard.py` is not a test of `core.py`, however much of the name it repeats.

    The subject match is anchored to the shapes a test is actually named with, because an
    unanchored `subject in path` deletes every file whose name spells another's.
    """
    repo = tmp_path / "stem"
    init_test_repo(repo)
    commit_files(
        repo,
        {
            "scripts/core.py": "def run():\n    return 1\n",
            "scripts/scorecard.py": "def score():\n    return 1\n",
            "scripts/select.sh": "echo picking\n",
            ".github/workflows/live.yaml": "run: bash scripts/select.sh\n",
        },
        "base",
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"], cwd=repo, env=git_env(), check=True
    )
    commit_files(
        repo,
        {
            "scripts/core.py": "def run():\n    return 2\n",
            "scripts/scorecard.py": "def score():\n    return 2\n",
        },
        "polish both",
    )
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=repo, env=git_env(), check=True
    )
    subprocess.run(
        ["git", "rm", "-q", "scripts/core.py", "scripts/scorecard.py"],
        cwd=repo,
        env=git_env(),
        check=True,
    )
    commit_files(repo, {}, "drop both")
    _merge_to_conflict(repo, "feature")
    decided = _decide(repo, ["scripts/core.py", "scripts/scorecard.py"])
    # Both are unreferenced, so the reference rule takes both. What must NOT happen is
    # scorecard.py being taken as a TEST of core.py, which is a different rule and a wrong one.
    for path, found in decided.items():
        assert found.rule != "follows-a-deleted-subject", (path, found)


def test_a_workflow_file_is_never_decided_by_reference(tmp_path):
    """A deleted required check reports nothing and hangs the PR, so the rule's safety
    argument — a wrong deletion goes red — does not hold under `.github/workflows/`."""
    repo = tmp_path / "workflow"
    init_test_repo(repo)
    commit_files(
        repo,
        {
            ".github/workflows/orphan.yaml": "on: pull_request\njobs: {a: {runs-on: x}}\n",
            ".github/workflows/named.yaml": "on: pull_request\njobs: {b: {runs-on: x}}\n",
            "docs/ci.md": "the battery runs .github/workflows/named.yaml\n",
        },
        "base",
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"], cwd=repo, env=git_env(), check=True
    )
    commit_files(
        repo,
        {
            ".github/workflows/orphan.yaml": "on: pull_request\njobs: {a: {runs-on: y}}\n"
        },
        "retune the orphan",
    )
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=repo, env=git_env(), check=True
    )
    subprocess.run(
        ["git", "rm", "-q", ".github/workflows/orphan.yaml"],
        cwd=repo,
        env=git_env(),
        check=True,
    )
    commit_files(repo, {}, "drop the orphan workflow")
    _merge_to_conflict(repo, "feature")
    assert _decide(repo, [".github/workflows/orphan.yaml"]) == {}


def test_a_caller_that_is_itself_conflicted_still_counts_as_a_caller(tmp_path):
    """The exclusion set names what this pass can DELETE, never every conflicted path.

    A both-modified conflict SURVIVES the merge. Excluding it from the reference search hides
    whatever it names, so a module whose one live caller happens to sit in another conflicted
    file would read as unreferenced and be deleted out from under that caller.
    """
    repo = tmp_path / "conflicted-caller"
    init_test_repo(repo)
    commit_files(
        repo,
        {
            "scripts/foo.py": "def run():\n    return 1\n",
            "scripts/bar.py": "import foo\n\nVALUE = 1\n",
            "scripts/select.sh": "echo picking\n",
            ".github/workflows/live.yaml": "run: bash scripts/select.sh\n",
        },
        "base",
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"], cwd=repo, env=git_env(), check=True
    )
    commit_files(
        repo,
        {
            "scripts/foo.py": "def run():\n    return 2\n",
            "scripts/bar.py": "import foo\n\nVALUE = 2\n",
        },
        "edit both",
    )
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=repo, env=git_env(), check=True
    )
    commit_files(
        repo, {"scripts/bar.py": "import foo\n\nVALUE = 3\n"}, "edit the caller"
    )
    subprocess.run(
        ["git", "rm", "-q", "scripts/foo.py"], cwd=repo, env=git_env(), check=True
    )
    commit_files(repo, {}, "drop foo")
    _merge_to_conflict(repo, "feature")
    # bar.py is a both-modified conflict AND the only thing importing foo. Both paths are
    # handed in together, exactly as prepare.sh hands the whole unmerged list.
    assert _decide(repo, ["scripts/foo.py", "scripts/bar.py"]) == {}


def test_no_rule_decides_a_workflow_even_by_following_a_subject(tmp_path):
    """The prefix guard belongs to EVERY rule, not to the reference rule alone.

    `.github/workflows/test-closure.yaml` is named as a test of a decided `closure.py`, and a
    workflow file is named by nothing, so `follows-a-deleted-subject` would take it — while
    the guard exists because a deleted required check hangs the pull request instead of going
    red, which is exactly the argument that rule cannot make either.
    """
    repo = tmp_path / "workflow-follower"
    init_test_repo(repo)
    commit_files(
        repo,
        {
            "scripts/closure.py": "def run():\n    return 1\n",
            ".github/workflows/test-closure.yaml": "on: pull_request\njobs: {a: {runs-on: x}}\n",
            "scripts/select.sh": "echo picking\n",
            "docs/how.md": "the battery runs scripts/select.sh\n",
        },
        "base",
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"], cwd=repo, env=git_env(), check=True
    )
    commit_files(
        repo,
        {
            "scripts/closure.py": "def run():\n    return 2\n",
            ".github/workflows/test-closure.yaml": "on: pull_request\njobs: {a: {runs-on: y}}\n",
        },
        "polish both",
    )
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=repo, env=git_env(), check=True
    )
    subprocess.run(
        [
            "git",
            "rm",
            "-q",
            "scripts/closure.py",
            ".github/workflows/test-closure.yaml",
        ],
        cwd=repo,
        env=git_env(),
        check=True,
    )
    commit_files(repo, {}, "drop both")
    _merge_to_conflict(repo, "feature")
    decided = _decide(
        repo, ["scripts/closure.py", ".github/workflows/test-closure.yaml"]
    )
    assert ".github/workflows/test-closure.yaml" not in decided, decided


def test_a_short_appended_bullet_keeps_the_fragment(tmp_path):
    """A bullet too short to be evidence is still a line the release never carried.

    `_entries`' threshold answers "is this text evidence a release shipped it?". The premise
    that nothing is lost is a different question, and counting it with that threshold deletes
    an appended `- Also fix the flag.` unexamined.
    """
    repo = tmp_path / "short-bullet"
    init_test_repo(repo)
    commit_files(
        repo,
        {_FRAGMENT: f"- {_ENTRY}\n", "CHANGELOG.md": "# Changelog\n"},
        "base",
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"], cwd=repo, env=git_env(), check=True
    )
    commit_files(
        repo,
        {_FRAGMENT: f"- {_ENTRY}\n- Also fix the flag.\n"},
        "append a short bullet",
    )
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=repo, env=git_env(), check=True
    )
    subprocess.run(["git", "rm", "-q", _FRAGMENT], cwd=repo, env=git_env(), check=True)
    commit_files(
        repo,
        {"CHANGELOG.md": f"# Changelog\n\n## [0.55.0]\n\n### Changed\n\n- {_ENTRY}\n"},
        "release v0.55.0",
    )
    _merge_to_conflict(repo, "feature")
    assert _decide(repo, [_FRAGMENT]) == {}


def test_one_named_sibling_does_not_make_a_glob_directory_reachable(tmp_path):
    """A mixed directory holds an explicitly launched file beside glob-loaded ones.

    `plugins/bar.py` is named by the launcher while every other plugin is discovered, so one
    named sibling would license deleting all of them. Most siblings being named is what says
    the directory's convention is naming rather than discovery.
    """
    repo = tmp_path / "mixed"
    init_test_repo(repo)
    plugins = {f"plugins/p{n}.py": f"def run():\n    return {n}\n" for n in range(1, 6)}
    commit_files(
        repo,
        {
            **plugins,
            "plugins/bar.py": "def run():\n    return 0\n",
            # Only ONE sibling is named; the loader takes the rest by glob.
            "app.py": "import bar\n\nload_all('plugins/*.py')\n",
        },
        "base",
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "feature"], cwd=repo, env=git_env(), check=True
    )
    commit_files(
        repo, {"plugins/p1.py": "def run():\n    return 99\n"}, "polish a plugin"
    )
    subprocess.run(
        ["git", "checkout", "-q", "main"], cwd=repo, env=git_env(), check=True
    )
    subprocess.run(
        ["git", "rm", "-q", "plugins/p1.py"], cwd=repo, env=git_env(), check=True
    )
    commit_files(repo, {}, "drop a plugin")
    _merge_to_conflict(repo, "feature")
    assert _decide(repo, ["plugins/p1.py"]) == {}
