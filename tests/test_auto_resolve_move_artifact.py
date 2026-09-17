"""The MOVE ARTIFACT: a conflict region git wrote by lining up text one side
MOVED against different text on the other side.

agent-glovebox#6247: both branches moved the same three definitions in opposite
directions and both appended a new test case. Git matched one side's function
body against the other side's new case, so the region carried 300 lines on one
side, 128 on the other, no base lines, and no line either side shared. The shard
spent its whole SHARD_TIMEOUT_SECONDS on a block that held no answer.

Each case builds a real scratch repository and drives a real `git merge`, so the
marker spellings and the place git cuts are git's own.
"""

# covers: .github/resolver/auto-resolve/_conflict_hunks.py
# covers: .github/resolver/auto-resolve/fanout.py
# covers: .github/resolver/auto-resolve/prompts.py
# covers: .github/resolver/auto-resolve/_marker_verdict.py

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import commit_files, git_env, git_out, init_test_repo
from tests._resolver_helpers import REPO_ROOT, load_script, record_gh_call

hunks = load_script(".github/resolver/auto-resolve/_conflict_hunks.py")
fanout = load_script(".github/resolver/auto-resolve/fanout.py")
marker_verdict = load_script(".github/resolver/auto-resolve/_marker_verdict.py")
git_io = sys.modules["_git_io"]
denials = sys.modules["_denials"]

_MARKS = json.loads(
    (REPO_ROOT / ".github/resolver/lib/shared-names.json").read_text(encoding="utf-8")
)["commit_status_marks"]

FILE = "pkg/defs.py"
_ALPHA = "def alpha():\n    return 1"
_BETA = "def beta():\n    return 2"
_GAMMA = "def gamma():\n    return 3"
_TAIL = "def tail():\n    return 0"
_PR_CASE = "def added_on_the_pr():\n    return 24"
_BASE_CASE = "def added_on_the_base():\n    return 25"


def _module(*definitions: str) -> str:
    return "\n\n".join(definitions) + "\n"


def _merged(repo: Path, ours: str, theirs: str) -> Path:
    """REPO parked mid-merge, with OURS on the checked-out branch and THEIRS on
    the branch merged into it — HEAD and MERGE_HEAD, as the resolver reads them.

    diff3 is the style the resolver's own `lib.sh` configures, and the only one
    that writes the `|||||||` section every reader here needs.
    """
    init_test_repo(repo)
    git_out(repo, "config", "merge.conflictStyle", "diff3")
    commit_files(repo, {FILE: _module(_ALPHA, _BETA, _GAMMA, _TAIL)}, "the base")
    git_out(repo, "checkout", "-q", "-b", "base-side")
    commit_files(repo, {FILE: theirs}, "the base branch")
    git_out(repo, "checkout", "-q", "main")
    commit_files(repo, {FILE: ours}, "the pull request")
    subprocess.run(
        ["git", "merge", "--no-commit", "base-side"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        check=False,
    )
    return repo


def _moved_repo(repo: Path) -> Path:
    """The issue's shape. The pull request moves the three definitions below the
    tail and appends one; the base branch leaves them where they are and appends
    one above them."""
    return _merged(
        repo,
        ours=_module(_TAIL, _ALPHA, _BETA, _GAMMA, _PR_CASE),
        theirs=_module(_BASE_CASE, _ALPHA, _BETA, _GAMMA, _TAIL),
    )


def _one_block(repo: Path) -> "hunks.Hunk":
    text = (repo / FILE).read_text(encoding="utf-8")
    blocks = hunks.hunks_of(text)
    assert len(blocks) == 1, f"the fixture must conflict in exactly one block: {text}"
    return blocks[0]


def _parents(repo: Path) -> tuple[str, str]:
    return (
        git_out(repo, "show", f"HEAD:{FILE}"),
        git_out(repo, "show", f"MERGE_HEAD:{FILE}"),
    )


def test_a_block_both_sides_moved_is_a_move_artifact(tmp_path):
    """The premise and the verdict together: taking either side alone drops a
    definition the other parent holds, so the block carries no answer — and that
    is what the detector must see."""
    repo = _moved_repo(tmp_path / "repo")
    block = _one_block(repo)
    text = (repo / FILE).read_text(encoding="utf-8")
    taking_ours = hunks.splice(
        text, {block.ordinal: hunks.side_of(block.text, hunks.OURS)}
    )
    taking_theirs = hunks.splice(
        text, {block.ordinal: hunks.side_of(block.text, hunks.THEIRS)}
    )
    assert "def added_on_the_base" not in taking_ours
    assert "def tail" not in taking_theirs

    assert hunks.is_move_artifact(block.text, *_parents(repo))


def test_a_block_both_sides_EDITED_is_not_a_move_artifact(tmp_path):
    """The ordinary conflict. The base section says what each side edited, so
    the block holds its own answer and needs no parent file."""
    repo = _merged(
        tmp_path / "repo",
        ours=_module("def alpha():\n    return 11", _BETA, _GAMMA, _TAIL),
        theirs=_module("def alpha():\n    return 12", _BETA, _GAMMA, _TAIL),
    )
    block = _one_block(repo)
    assert hunks.sides_of(block.text).base.strip() != ""
    assert not hunks.is_move_artifact(block.text, *_parents(repo))


def test_two_sides_ADDING_different_definitions_is_not_a_move_artifact(tmp_path):
    """The near miss: no base lines and no shared line either, but neither side's
    lines sit anywhere in the other parent, so nothing moved."""
    repo = _merged(
        tmp_path / "repo",
        ours=_module(_ALPHA, _BETA, _GAMMA, _PR_CASE, _TAIL),
        theirs=_module(_ALPHA, _BETA, _GAMMA, _BASE_CASE, _TAIL),
    )
    block = _one_block(repo)
    assert hunks.sides_of(block.text).base.strip() == ""
    assert not hunks.is_move_artifact(block.text, *_parents(repo))


def test_a_path_only_ONE_parent_has_is_not_a_move_artifact(tmp_path):
    """A path git added or renamed on one side has no version at the other ref,
    and `_parent_text` answers "" for it. Both parents decide a move, so an
    absent one refuses in EITHER direction — otherwise the shard is handed an
    empty parent file and told the answer is in it."""
    repo = _moved_repo(tmp_path / "repo")
    block = _one_block(repo)
    ours, theirs = _parents(repo)
    assert hunks.is_move_artifact(block.text, ours, theirs)

    assert not hunks.is_move_artifact(block.text, "", theirs)
    assert not hunks.is_move_artifact(block.text, ours, "")


def test_a_run_its_OWN_parent_repeats_is_not_a_move_artifact(tmp_path):
    """The duplicated-text near miss. A run the file already holds twice is text
    that file repeats, so finding a copy of it across the merge says nothing
    about where either side put it."""
    repo = _moved_repo(tmp_path / "repo")
    block = _one_block(repo)
    ours, theirs = _parents(repo)
    duplicated_ours = f"{ours}\n{hunks.side_of(block.text, hunks.OURS)}"
    duplicated_theirs = f"{theirs}\n{hunks.side_of(block.text, hunks.THEIRS)}"

    assert not hunks.is_move_artifact(block.text, duplicated_ours, duplicated_theirs)


def test_the_shard_for_a_moved_block_is_handed_both_parent_files(tmp_path, monkeypatch):
    """The wiring. The shard cannot run git, so the two parents reach it as files
    it may read, named in its own prompt."""
    repo = _moved_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    plan = fanout.Fanout()
    plan.files = [FILE]
    plan.pr_number = "6247"
    plan.dir = tmp_path / "logs"

    plan.plan_work()

    work = plan.work[0]
    assert work.hunk.move_artifact
    ours, theirs = _parents(repo)
    assert Path(work.hunk.ours_parent_path).read_text(encoding="utf-8").strip() == ours
    assert (
        Path(work.hunk.theirs_parent_path).read_text(encoding="utf-8").strip() == theirs
    )
    prompt = plan.shard_prompt_for(0, work)
    assert work.hunk.ours_parent_path in prompt
    assert work.hunk.theirs_parent_path in prompt
    assert "Match them by NAME" in prompt
    # The shard may READ those two paths, and the write grant still names only
    # its own file: a fork head's reads are otherwise confined to the merged tree.
    config = tmp_path / "config"
    config.mkdir()
    grants = plan.write_shard_settings(config, 0, work)
    assert grants.readable.splitlines() == [
        work.hunk.ours_parent_path,
        work.hunk.theirs_parent_path,
    ]
    assert grants.target == plan.resolved_path(0)


def _refusal_for(repo: Path, tmp_path: Path, monkeypatch) -> Path:
    """The leftover-marker refusal for a run whose one shard was killed at
    SHARD_TIMEOUT_SECONDS, and the log the stubbed `gh` recorded it in."""
    monkeypatch.chdir(repo)
    git_io.bind_repo(repo)
    fanout_dir = tmp_path / "fanout"
    fanout_dir.mkdir()
    shard = {"file": FILE, "resolved": False, "is_error": 1, "timed_out": True}
    (fanout_dir / "execution.json").write_text(
        json.dumps({"shards": [shard]}), encoding="utf-8"
    )
    monkeypatch.setenv("FANOUT_DIR", str(fanout_dir))
    monkeypatch.setenv("PR", "1")
    monkeypatch.setenv("GH_REPO", "owner/repo")
    monkeypatch.setenv("HEAD_SHA", git_out(repo, "rev-parse", "HEAD"))
    monkeypatch.setenv("BUNDLE_DIR", str(tmp_path / "bundle"))
    for name in (
        "LLM_PERMISSION_DENIALS",
        "LLM_PERMISSION_DENIED_TOOLS",
        "LLM_PERMISSION_DENIALS_BY_FILE",
        "DEFERRED_REGEN",
    ):
        monkeypatch.delenv(name, raising=False)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "gh.log"
    stub = binaries / "gh"
    stub.write_text(
        "#!/usr/bin/env bash\n" + record_gh_call(str(log)) + "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")
    verdict = marker_verdict.MarkerVerdict(
        allowed=[FILE],
        denials=denials.Denials.from_env(),
        pr="1",
        bundle_dir=tmp_path / "bundle",
        checked_out_head=git_out(repo, "rev-parse", "HEAD"),
        merge_base_side=git_out(repo, "rev-parse", "MERGE_HEAD"),
    )
    with pytest.raises(SystemExit):
        verdict.refuse_leftover_markers(".")
    return log


def test_a_move_artifact_that_starved_a_shard_DECLINES_on_the_first_timeout(
    tmp_path, monkeypatch, capsys
):
    """Every other spent shard is handed off, and declines only on a second
    sighting of the same cause. This one declines at once: the hunk holds no
    answer, so the next run under the same bound stops in the same place."""
    comment = _refusal_for(_moved_repo(tmp_path / "repo"), tmp_path, monkeypatch)
    published = comment.read_text(encoding="utf-8")
    assert f"context={_MARKS['auto_resolve_declined']}" in published
    assert _MARKS["auto_resolve_handoff"] not in published
    assert "Both sides MOVED a run of definitions" in published
    capsys.readouterr()


def test_an_ordinary_starved_shard_is_still_handed_off(tmp_path, monkeypatch, capsys):
    """The other side of the same branch, so the decline above is the move
    artifact's doing and not the timeout's."""
    repo = _merged(
        tmp_path / "repo",
        ours=_module("def alpha():\n    return 11", _BETA, _GAMMA, _TAIL),
        theirs=_module("def alpha():\n    return 12", _BETA, _GAMMA, _TAIL),
    )
    published = _refusal_for(repo, tmp_path, monkeypatch).read_text(encoding="utf-8")
    assert f"context={_MARKS['auto_resolve_handoff']}" in published
    assert _MARKS["auto_resolve_declined"] not in published
    capsys.readouterr()
