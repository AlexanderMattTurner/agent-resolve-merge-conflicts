"""The bundle step's own functions, driven IN THIS INTERPRETER.

`tests/test_auto_resolve_bundle_equivalence.py` and
`.github/resolver/auto-resolve/bundle.test.mjs` both drive the step as a
subprocess, which is the only way to pin its bytes and its git side effects — and
coverage cannot trace into a child interpreter, so neither measures a single
line. This module closes that: it imports the script and calls its functions
directly, so every refusal and every parse rule is both exercised and measured.

The equivalence corpus stays the authority on WHAT the step outputs. These tests
assert the decisions behind those bytes, one branch at a time.
"""

# covers: .pre-commit-config.yaml
# covers: .github/workflows/auto-resolve.yaml
# covers: .github/resolver/auto-resolve/bundle.test.mjs
# covers: .github/resolver/auto-resolve/_marker_verdict.py
# covers: .github/resolver/auto-resolve/_post_merge_check.py

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from tests._fake_github import FakeHeadRuns
from tests._resolver_helpers import (
    REPO_ROOT,
    SYSTEM_PATH_DIRS,
    load_script,
    path_without_binary,
    record_gh_call,
    status_comments,
)

# The ladder's variable names, in attempt order. bundle.py holds no copy — it runs
# lib/oauth-ladder.bash — so a test that must set or blank a rung reads the same
# definition that walk reads.
_LADDER_VARS = tuple(
    json.loads(
        (REPO_ROOT / ".github" / "resolver" / "lib" / "shared-names.json").read_text(
            encoding="utf-8"
        )
    )["oauth_ladder_vars"]
)

# The two commit-status marks a leftover-marker refusal can leave, read from the
# file the shell writer reads: which one it is decides whether a later resolver
# change re-opens the pull request.
_MARKS = json.loads(
    (REPO_ROOT / ".github" / "resolver" / "lib" / "shared-names.json").read_text(
        encoding="utf-8"
    )
)["commit_status_marks"]

bundle = load_script(".github/resolver/auto-resolve/bundle.py")
# The module bundle imported RepairPass FROM, not a second copy of it: the repair
# spawn resolves its script path there, so a test redirecting that path patches the
# instance the step actually inherits.
repair_pass = sys.modules["_repair_pass"]
credentials = sys.modules["_credentials"]
# The step's own seams, driven where they live rather than through the names
# bundle.py imports: git_io runs git and undoes the merge, denials reads what the
# execution log said about permission denials, and hook_gate reads the repo's
# pre-commit hooks. Read them out of sys.modules, where bundle.py's own
# `from _git_io import …` above registered them: load_script re-executes the file
# into a FRESH module, so loading them here again would build second copies, and a
# monkeypatch on a copy patches a module the step never calls.
git_io = sys.modules["_git_io"]
setup_record = sys.modules["_setup_record"]
denials = sys.modules["_denials"]
hook_gate = sys.modules["_hook_gate"]
self_review_gate = sys.modules["_self_review_gate"]
refusal = sys.modules["_refusal"]
marker_verdict = sys.modules["_marker_verdict"]

CONFLICTED = "a.md"

# One hook whose entry resolves the project environment (`uv run`) and one that
# does not, so the refusal below is checked in both directions.
PRECOMMIT_FIXTURE = (
    "repos:\n"
    "  - repo: local\n"
    "    hooks:\n"
    "      - id: resolves-the-project-env\n"
    "        entry: uv run pytest tests/test_x.py -q\n"
    "      - id: plain-system-hook\n"
    "        entry: python3 .github/scripts/checks/x.py\n"
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    ).stdout


def _enter_repo(work: Path, monkeypatch) -> Path:
    """Point both the process and `_git_io` at WORK, and return it.

    The bind is what keeps a destructive call inside the fixture repository:
    `_git_io` refuses an unbound call rather than falling back to the process
    working directory, which for this suite is the developer's own checkout.
    monkeypatch restores the module afterwards, so a later test that forgets to
    bind gets the refusal instead of a stranger's tree.
    """
    monkeypatch.chdir(work)
    # The real bind, not a poke at `_REPO`: it is what bundle.py calls, so this
    # exercises the resolution the step depends on rather than a stand-in for it.
    # setattr first so monkeypatch restores the module afterwards.
    monkeypatch.setattr(git_io, "_REPO", None)
    git_io.bind_repo(work)
    return work


def test_bind_repo_refuses_a_path_outside_any_repository(tmp_path, monkeypatch):
    """`bind_repo` resolves through `git -C <path> rev-parse --show-toplevel`,
    so a path git itself refuses (not a repository) must exit loudly rather
    than bind to whatever `rev-parse` printed on stderr."""
    monkeypatch.setattr(git_io, "_REPO", None)
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    with pytest.raises(SystemExit):
        git_io.bind_repo(outside)
    assert git_io._REPO is None  # pylint: disable=protected-access


def test_bound_repo_refuses_when_unbound(monkeypatch):
    """The unbound state is what keeps a later call from silently reaching the
    last run's checkout — `bound_repo` must refuse rather than guess."""
    monkeypatch.setattr(git_io, "_REPO", None)
    with pytest.raises(RuntimeError, match="unbound"):
        git_io.bound_repo()


def test_reset_process_state_clears_a_prior_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(git_io, "_REPO", None)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    git_io.bind_repo(repo)
    git_io._reset_process_state()  # pylint: disable=protected-access
    with pytest.raises(RuntimeError, match="unbound"):
        git_io.bound_repo()


# The conflicted file's body at the base, on the feature side and on the base
# side. A case that needs lines OUTSIDE the conflict block passes its own three.
CONFLICTED_BODIES = ("base\n", "feature side\n", "main side\n")


def _repo(
    tmp_path: Path,
    extra: dict[str, str] | None = None,
    main_extra: dict[str, str] | None = None,
    bodies: tuple[str, str, str] = CONFLICTED_BODIES,
) -> Path:
    """A repository parked mid-merge on one conflicted path, which is the state
    prepare hands this step."""
    work = tmp_path / "work"
    work.mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "commit.gpgsign", "false")
    # The identity goes in the repository's own config, not just in `_git`'s
    # environment: the subject calls `git commit` itself, in this interpreter,
    # and a machine with no global identity would otherwise fail that call.
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@e")
    # `untouched.md` and `other.md` are tracked and never conflicted, so a test
    # about an edit OUTSIDE the resolved set has a file to edit. They cannot be
    # committed later: the tree is mid-merge, and git refuses a commit there.
    # The step reads this to decide which hooks it must refuse to run. It
    # declares one hook of each kind so the refusal has something to select and
    # something to leave alone.
    files = {
        CONFLICTED: bodies[0],
        "untouched.md": "base\n",
        "other.md": "base\n",
        ".pre-commit-config.yaml": PRECOMMIT_FIXTURE,
    }
    for name, body in {**files, **(extra or {})}.items():
        (work / name).write_text(body, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "checkout", "-q", "-b", "feature")
    (work / CONFLICTED).write_text(bodies[1], encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "feature")
    _git(work, "checkout", "-q", "main")
    (work / CONFLICTED).write_text(bodies[2], encoding="utf-8")
    # `main_extra` is how a test gives the BASE side a landed change the feature
    # branch never touched — the shape a decline would revert.
    for name, body in (main_extra or {}).items():
        (work / name).write_text(body, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "main change")
    # is_unmergeable (bundle.py) reads BASE_REF's attributes from
    # origin/BASE_REF, matching what prepare.sh's own fetch leaves in the
    # workspace — a local-only "main" is not what the step queries.
    _git(work, "update-ref", "refs/remotes/origin/main", "main")
    _git(work, "checkout", "-q", "feature")
    subprocess.run(
        ["git", "-C", str(work), "merge", "--no-edit", "main"],
        capture_output=True,
        check=False,
    )
    return work


def _stub_gh(tmp_path: Path, monkeypatch) -> Path:
    """A `gh` on PATH that records its argv, so the refusal comment is readable
    without a network call."""
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    log = tmp_path / "gh.log"
    stub = binaries / "gh"
    # STUB_PR_HEAD is how a test says a push landed while the run was resolving:
    # the head read answers a SHA of the test's choosing, and every other call
    # keeps answering the empty stdout the refusal path already expects.
    stub.write_text(
        "#!/usr/bin/env bash\n"
        + record_gh_call(str(log))
        + 'if [[ "$*" == *.head.sha* ]]; then\n'
        '  printf "%s\\n" "${STUB_PR_HEAD:-}"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")
    return log


def _bundle_step(tmp_path, monkeypatch, work: Path, conflict_list: str):
    """A Bundle wired to the mid-merge repository WORK, resolving CONFLICT_LIST,
    with `gh` stubbed. One builder for every fixture repo below: a second copy of
    this environment drifts from the job's own, and only one of them is right."""
    _enter_repo(work, monkeypatch)
    monkeypatch.setenv("PR", "1")
    # The resolve job sets this for every step; the status comment builds its endpoint
    # from it.
    monkeypatch.setenv("GH_REPO", "owner/repo")
    # The commit discover dispatched this job with. The job checks that SHA out,
    # so the default here is what the checkout left at HEAD.
    monkeypatch.setenv("HEAD_SHA", _git(work, "rev-parse", "HEAD").strip())
    monkeypatch.setenv("BUNDLE_DIR", str(tmp_path / "bundle"))
    monkeypatch.setenv("CONFLICT_LIST", conflict_list)
    # refuse_unmergeable_paths reads this path's attributes off origin/BASE_REF;
    # every fixture repo creates that ref against "main", its base branch.
    monkeypatch.setenv("BASE_REF", "main")
    for name in (
        "MODIFY_DELETE_PATHS",
        "MODIFY_DELETE_VERDICTS",
        "SIDECAR_PATHS",
        "SIDECAR_RESOLUTIONS",
        "DEFERRED_REGEN",
        "LLM_PERMISSION_DENIALS",
        "LLM_PERMISSION_DENIED_TOOLS",
        "LLM_PERMISSION_DENIALS_BY_FILE",
        # The credential ladder: an ambient token would arm the self-review gate
        # and the hook-repair pass, and either could reach for a real model run.
        *_LADDER_VARS,
    ):
        monkeypatch.delenv(name, raising=False)
    _stub_gh(tmp_path, monkeypatch)
    return bundle.Bundle()


@pytest.fixture
def step(tmp_path, monkeypatch):
    """A Bundle wired to a fresh mid-merge repository, with `gh` stubbed."""
    return _bundle_step(tmp_path, monkeypatch, _repo(tmp_path), CONFLICTED)


# --- the two environment fields whose only job is to choose a wording ---------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("null", None),
        ('["Edit","Bash"]', ["Edit", "Bash"]),
        ("[]", []),
    ],
    ids=["unset", "explicit_null", "named", "empty"],
)
def test_denied_tools_reads_every_well_formed_shape(monkeypatch, raw, expected):
    monkeypatch.setenv("LLM_PERMISSION_DENIED_TOOLS", raw)
    assert denials.read_denied_tools() == expected


@pytest.mark.parametrize(
    "raw",
    ["Edit", "5", '{"a":1}', '["Edit",7]'],
    ids=["bare", "int", "object", "mixed"],
)
def test_a_malformed_denied_set_degrades_loudly(monkeypatch, capsys, raw):
    """A malformed value must not abort a resolution that is otherwise ready to
    bundle: this field only chooses the WORDING of a diagnosis."""
    monkeypatch.setenv("LLM_PERMISSION_DENIED_TOOLS", raw)
    assert denials.read_denied_tools() is None
    assert "is not a JSON array of tool names" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("null", None),
        ('{"a.md":["Edit"]}', {"a.md": ["Edit"]}),
        ("{}", {}),
    ],
    ids=["unset", "explicit_null", "attributed", "empty"],
)
def test_denials_by_file_reads_every_well_formed_shape(monkeypatch, raw, expected):
    monkeypatch.setenv("LLM_PERMISSION_DENIALS_BY_FILE", raw)
    assert denials.read_denials_by_file() == expected


@pytest.mark.parametrize(
    "raw",
    ["[]", "not json", '{"a.md":[123]}', '{"a.md":"Edit"}'],
    ids=["array", "corrupt", "non_string_element", "non_array_value"],
)
def test_a_malformed_attribution_degrades_loudly(monkeypatch, capsys, raw):
    """The ELEMENT type is load-bearing: a map whose values hold non-strings
    matches no edit-tool name, so accepting one would report "no denial landed on
    a marker file" — the LENIENT branch — off a plumbing bug."""
    monkeypatch.setenv("LLM_PERMISSION_DENIALS_BY_FILE", raw)
    assert denials.read_denials_by_file() is None
    assert "is not a JSON object of per-file tool arrays" in capsys.readouterr().out


def test_a_multiline_value_is_folded_into_one_line(monkeypatch, capsys):
    """A `::warning::` whose payload carries a newline lets the tail begin a line,
    and a line beginning `::` is a workflow command the runner executes."""
    monkeypatch.setenv("LLM_PERMISSION_DENIED_TOOLS", "not\njson\r\n")
    denials.read_denied_tools()
    warning = capsys.readouterr().out.strip()
    assert warning.count("\n") == 0
    assert "not json" in warning


@pytest.mark.parametrize(
    ("denied", "expected"),
    [
        (None, "not reported by the execution log"),
        ([], "none reported"),
        (["Bash", "Edit", "Bash"], "Bash, Edit"),
    ],
    ids=["unnamed", "empty", "deduplicated_and_sorted"],
)
def test_the_denied_set_is_rendered_for_a_human(denied, expected):
    assert denials.denied_tools_text(denied) == expected


@pytest.mark.parametrize(
    ("by_file", "expected"),
    [
        (None, True),
        ({"a.md": ["Edit"]}, True),
        ({"a.md": ["Bash"]}, False),
        ({"other.md": ["Edit"]}, False),
        ({}, False),
    ],
    ids=["unattributed", "on_the_marker_file", "non_edit", "elsewhere", "empty"],
)
def test_the_marker_file_join_answers_conservatively(by_file, expected):
    """Answers True — the conservative direction under the caller's `not` —
    whenever the attribution is absent, so an un-attributable log keeps the
    blocking diagnosis rather than gaining a cheerier one it cannot support."""
    assert denials.denials_blocked_a_marker_file(by_file, ["a.md"]) is expected


# --- the hook-report classification ------------------------------------------


@pytest.mark.parametrize(
    "report",
    ["Executable `shellcheck` not found\n", "- exit code: 127\n", "- exit code: 78\n"],
    ids=["missing_executable", "exit_127", "exit_78_skip"],
)
def test_a_hook_that_could_not_start_is_recognised(report):
    assert hook_gate.hook_could_not_run(report) is True


@pytest.mark.parametrize(
    "report",
    ["ruff.....Failed\n", "- exit code: 1\n", "Executable found\n", ""],
    ids=["rejected", "exit_1", "near_miss", "empty"],
)
def test_a_hook_that_judged_the_content_is_not_a_provisioning_fault(report):
    """A misclassification in either direction is a wording error, never a safety
    hole — but reporting a rejection as a provisioning fault would tell a human
    to audit their runner over a real lint failure."""
    assert hook_gate.hook_could_not_run(report) is False


# --- the environment list split ----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("a.md", ["a.md"]),
        ("a.md b.md", ["a.md", "b.md"]),
        ("  a.md \n", ["a.md"]),
    ],
    ids=["unset", "one", "two", "padded"],
)
def test_a_path_list_splits_the_way_the_shell_split_it(monkeypatch, raw, expected):
    monkeypatch.setenv("CONFLICT_LIST", raw)
    assert bundle.env_list("CONFLICT_LIST") == expected


# --- the parents, and the tree guards ----------------------------------------


def test_the_parents_come_from_the_open_merge(step):
    step.read_parents()
    assert step.merge_base_side == git_io.git("rev-parse", "main").strip()
    assert step.checked_out_head == git_io.git("rev-parse", "feature").strip()


def test_the_parents_come_from_a_merge_commit_when_the_merge_is_closed(step, tmp_path):
    """Prepare's clean-merge path: git already committed the merge, so reading
    MERGE_HEAD unconditionally would kill that path before any diagnosis."""
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    git_io.git("commit", "--no-edit", "--no-verify")
    step.read_parents()
    assert step.merge_base_side == git_io.git("rev-parse", "HEAD^2").strip()
    assert step.checked_out_head == git_io.git("rev-parse", "HEAD^").strip()


def test_a_tree_with_no_merge_at_all_is_named_as_plumbing(step, tmp_path):
    git_io.git("-C", str(tmp_path / "work"), "merge", "--abort")
    with pytest.raises(SystemExit):
        step.read_parents()
    assert "plumbing" in (tmp_path / "gh.log").read_text(encoding="utf-8")


def test_a_failure_on_a_head_a_push_replaced_summons_no_human(
    step, tmp_path, monkeypatch
):
    """A push — most often a human resolving the conflict by hand — lands while the
    run is resolving. The diagnosis is then about a commit that is no longer the
    head, so the alert telling a human to go resolve a conflict must not be posted.
    The job still exits non-zero, which is what keeps the diagnosis on the run."""
    monkeypatch.setenv("STUB_PR_HEAD", "f" * 40)
    bundle.git("-C", str(tmp_path / "work"), "merge", "--abort")
    with pytest.raises(SystemExit):
        step.read_parents()
    calls = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert ".head.sha" in calls
    assert status_comments(calls) == []


def test_a_failure_on_a_head_a_push_replaced_writes_no_handoff_mark(
    step, tmp_path, monkeypatch
):
    """The mark stands every later scan down until the head moves, and it is written
    against the commit this run READ. On a head a push already replaced, that spends
    the retry on a verdict about a tree nobody has, while the resolution this run
    paid for is discarded. The next scan resolves the new head instead."""
    monkeypatch.setenv("STUB_PR_HEAD", "f" * 40)
    with pytest.raises(SystemExit):
        bundle.fail("boom", "the diagnosis.")
    calls = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "auto-resolve/handed-off" not in calls
    assert status_comments(calls) == []


def test_a_failure_on_the_current_head_still_summons_a_human(
    step, tmp_path, monkeypatch
):
    """The no-false-negative side: when the PR's head is still the commit this run
    read, the refusal comment is the only thing that reaches a human, so suppressing
    it would hide a real conflict."""
    monkeypatch.setenv("STUB_PR_HEAD", bundle.git("rev-parse", "HEAD").strip())
    bundle.git("-C", str(tmp_path / "work"), "merge", "--abort")
    with pytest.raises(SystemExit):
        step.read_parents()
    assert "plumbing" in (tmp_path / "gh.log").read_text(encoding="utf-8")


def test_a_merge_this_run_committed_is_not_read_as_a_push(step, tmp_path, monkeypatch):
    """Prepare's clean-merge path leaves git's own merge commit at HEAD. That
    merge exists only in this runner, so a head read that answered from the local
    HEAD would report every refusal as a push that never happened."""
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    bundle.git("add", "--", CONFLICTED)
    bundle.git("commit", "--no-edit", "--no-verify")
    monkeypatch.setenv("STUB_PR_HEAD", os.environ["HEAD_SHA"])
    with pytest.raises(SystemExit):
        bundle.fail("boom", "the diagnosis.")
    assert status_comments((tmp_path / "gh.log").read_text(encoding="utf-8"))


def test_an_unset_dispatched_sha_still_summons_a_human(step, tmp_path, monkeypatch):
    """Suppressing a refusal needs evidence of a push, and an unset variable is
    evidence of nothing."""
    monkeypatch.delenv("HEAD_SHA")
    monkeypatch.setenv("STUB_PR_HEAD", "f" * 40)
    with pytest.raises(SystemExit):
        bundle.fail("boom", "the diagnosis.")
    assert status_comments((tmp_path / "gh.log").read_text(encoding="utf-8"))


def test_an_unreadable_pr_head_still_summons_a_human(step, tmp_path, monkeypatch):
    """An unknown head is not evidence of a push. `gh` answering nothing must leave
    the alert in place, or every read failure would silently swallow a refusal."""
    monkeypatch.delenv("STUB_PR_HEAD", raising=False)
    bundle.git("-C", str(tmp_path / "work"), "merge", "--abort")
    with pytest.raises(SystemExit):
        step.read_parents()
    assert "plumbing" in (tmp_path / "gh.log").read_text(encoding="utf-8")


def test_an_edit_outside_the_conflicted_set_is_refused(step):
    """The rail that bounds a protected-path resolution to the file that
    genuinely conflicted, and it holds regardless of what the model did."""
    (Path.cwd() / "untouched.md").write_text(
        "the resolver rewrote this\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit):
        step.refuse_edits_outside_the_set()


def test_a_new_untracked_file_is_refused(step):
    (Path.cwd() / "invented.md").write_text("invented\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        step.refuse_edits_outside_the_set()


def test_an_edit_to_a_file_this_pr_changed_is_accepted_and_staged(
    tmp_path, monkeypatch
):
    """WRITABLE_LIST is prepare's list of the head's own unconflicted changes. An
    edit there is recorded as widened, so it is staged beside the resolutions and
    named on the pull request rather than refused as a stray."""
    monkeypatch.setenv("WRITABLE_LIST", "untouched.md")
    widened = _bundle_step(tmp_path, monkeypatch, _repo(tmp_path), CONFLICTED)
    (Path.cwd() / "untouched.md").write_text("reached in\n", encoding="utf-8")
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    widened.refuse_edits_outside_the_set()
    assert widened.widened == ["untouched.md"]
    widened.stage_text_resolutions()
    assert widened.staged == [CONFLICTED, "untouched.md"]
    widened.stage_widened_edits()
    assert "untouched.md" in bundle.git_lines("diff", "--cached", "--name-only")


def test_a_widened_edit_that_only_re_spaces_the_merge_is_put_back(
    tmp_path, monkeypatch
):
    """A widened path merged cleanly, so both parents wrote its content. An edit
    there that changes only whitespace ports nothing, and it would land where the
    PR's own diff does not show it (agent-glovebox #5406, #5408)."""
    monkeypatch.setenv("WRITABLE_LIST", "untouched.md other.md")
    step = _bundle_step(tmp_path, monkeypatch, _repo(tmp_path), CONFLICTED)
    # `base\n` is what the merge left at both paths. One edit re-spaces it; the
    # other says something the merge does not.
    (Path.cwd() / "untouched.md").write_text("base   \n", encoding="utf-8")
    (Path.cwd() / "other.md").write_text("ported\n", encoding="utf-8")
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    step.refuse_edits_outside_the_set()
    step.stage_text_resolutions()
    step.stage_widened_edits()
    assert step.widened == ["other.md"]
    assert Path("untouched.md").read_text(encoding="utf-8") == "base\n"
    staged = bundle.git_lines("diff", "--cached", "--name-only")
    assert "other.md" in staged and "untouched.md" not in staged


def test_a_declined_shards_companion_edit_is_put_back(tmp_path, monkeypatch):
    """The hook logs each widened edit per shard. An edit only a shard that then
    DECLINED made accompanies a resolution that never landed, so it goes back to
    the merge's own content; one a resolving shard also made stays."""
    monkeypatch.setenv("WRITABLE_LIST", "untouched.md other.md")
    step = _declined_fixture(tmp_path, monkeypatch)
    for index, name in enumerate((CONFLICTED, "b.md")):
        shard = json.loads(
            (tmp_path / "fanout" / "execution.json").read_text(encoding="utf-8")
        )
        shard["shards"][index]["index"] = index
        (tmp_path / "fanout" / "execution.json").write_text(
            json.dumps(shard), encoding="utf-8"
        )
    for name in ("untouched.md", "other.md"):
        (Path.cwd() / name).write_text("reached in\n", encoding="utf-8")
    # Shard 0 (resolved a.md) edited other.md; shard 1 (declined b.md) edited both.
    (tmp_path / "fanout" / "0.widened").write_text(
        f"{Path.cwd()}/other.md\n", encoding="utf-8"
    )
    (tmp_path / "fanout" / "1.widened").write_text(
        f"{Path.cwd()}/untouched.md\n{Path.cwd()}/other.md\n", encoding="utf-8"
    )
    step.widened = ["untouched.md", "other.md"]
    step.staged += step.widened
    step.salvage_declined_paths()
    step.stage_widened_edits()
    assert step.widened == ["other.md"]
    assert Path("untouched.md").read_text(encoding="utf-8") == "base\n"
    staged = bundle.git_lines("diff", "--cached", "--name-only")
    assert "other.md" in staged and "untouched.md" not in staged


def test_a_writable_list_does_not_admit_a_file_outside_it(tmp_path, monkeypatch):
    monkeypatch.setenv("WRITABLE_LIST", "untouched.md")
    widened = _bundle_step(tmp_path, monkeypatch, _repo(tmp_path), CONFLICTED)
    (Path.cwd() / "other.md").write_text("strayed\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        widened.refuse_edits_outside_the_set()


def test_a_resolution_confined_to_the_conflicted_set_passes(step):
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    step.refuse_edits_outside_the_set()


# Lines both sides leave byte-identical, so a resolution has context OUTSIDE the
# conflict block to touch. `CONFLICTED_BODIES` is one line per commit, and every
# line of it lands inside the block.
CONTEXTFUL_BODIES = (
    "keep me\ndrop me\nbase body\ncontext\ntail\n",
    "keep me\ndrop me\nfeature body\ncontext\ntail\n",
    "keep me\ndrop me\nmain body\ncontext\ntail\n",
)


def test_an_ambiguous_revert_lands_the_content_and_reports_it(tmp_path, monkeypatch):
    """A revert that would have to guess costs the pull request nothing: the hunks
    the run resolved were sound, so the content lands and `land` names the lines.
    The name it prints has to be a line that EXISTS. A deletion contributes no
    resolved lines, so the resolved-side range is empty and reads backwards —
    agent-glovebox PR #4992 was handed off as "line(s) 32-31", which names nothing
    in either file."""
    step = _bundle_step(
        tmp_path, monkeypatch, _repo(tmp_path, bodies=CONTEXTFUL_BODIES), CONFLICTED
    )
    step.read_parents()
    # The shape #4992 hit: the block resolves to one side, and the resolution also
    # drops the line its own answer left unused. The revert cannot undo that alone,
    # because `drop me` is a line the mechanical merge holds outside every span.
    resolved = "keep me\nfeature body\ncontext\ntail\n"
    (Path.cwd() / CONFLICTED).write_text(resolved, encoding="utf-8")
    step.revert_out_of_conflict_rewrites()
    assert (Path.cwd() / CONFLICTED).read_text(encoding="utf-8") == resolved
    log = tmp_path / "gh.log"
    handoff = log.read_text(encoding="utf-8") if log.exists() else ""
    assert status_comments(handoff) == []
    mechanical = git_io.git(
        "show",
        f"{_mechanical_tree(step)}:{CONFLICTED}",
    ).splitlines()
    assert mechanical[1] == "drop me"
    assert step.out_of_conflict_rewrites == [f"{CONFLICTED}\t2"]


def test_a_rewrite_outside_the_block_is_reverted_and_the_run_goes_on(
    tmp_path, monkeypatch, capsys
):
    """A tidy-up the shard had no licence to make costs the PR nothing: outside the
    block both parents wrote the same bytes, so the mechanical merge is the content
    and the resolved hunk still stands."""
    step = _bundle_step(
        tmp_path, monkeypatch, _repo(tmp_path, bodies=CONTEXTFUL_BODIES), CONFLICTED
    )
    step.read_parents()
    resolved = Path.cwd() / CONFLICTED
    # `tail` sits two lines below the block, far enough that difflib reports the
    # re-indent as its own opcode rather than folding it into the block's.
    resolved.write_text(
        "keep me\ndrop me\nfeature body\ncontext\n    tail\n", encoding="utf-8"
    )
    step.revert_out_of_conflict_rewrites()
    reverted = "keep me\ndrop me\nfeature body\ncontext\ntail\n"
    assert resolved.read_text(encoding="utf-8") == reverted
    # The INDEX is what the merge commit takes, so a revert the working tree alone
    # carries would bundle the text this refusal just rejected.
    assert git_io.git("show", f":{CONFLICTED}") == reverted
    # The annotation is the revert's ONLY record — no status comment, no diff a
    # reviewer reads. It has to name the path and a MECHANICAL line.
    # A revert that needed no judgement reports NOTHING to `land`. Recording one
    # would turn auto-merge off on every tidy-up the run already undid.
    assert step.out_of_conflict_rewrites == []
    warning = capsys.readouterr().out
    assert "::warning::reverted" in warning
    assert f"'{CONFLICTED}'" in warning
    mechanical = git_io.git("show", f"{_mechanical_tree(step)}:{CONFLICTED}")
    assert "mechanical line(s) 9" in warning, warning
    assert mechanical.splitlines()[8] == "tail"


def _mechanical_tree(step) -> str:
    """The tree `git merge-tree` writes for the step's two parents — the text the
    refusal's line numbers are measured against."""
    return git_io.git(
        "-c",
        "merge.conflictStyle=merge",
        "merge-tree",
        "--write-tree",
        step.checked_out_head,
        step.merge_base_side,
        check=False,
    ).split("\n", 1)[0]


def test_an_unmergeable_path_in_the_list_is_refused(tmp_path, monkeypatch):
    """A `-merge` path cannot be resolved by editing, so naming one in the list is
    a plumbing defect rather than a resolution to stage."""
    _enter_repo(
        _repo(tmp_path, extra={".gitattributes": "b.md -merge\n", "b.md": "base b\n"}),
        monkeypatch,
    )
    monkeypatch.setenv("PR", "1")
    # The resolve job sets this for every step; the status comment builds its endpoint
    # from it.
    monkeypatch.setenv("GH_REPO", "owner/repo")
    monkeypatch.setenv("BUNDLE_DIR", str(tmp_path / "bundle"))
    monkeypatch.setenv("CONFLICT_LIST", f"{CONFLICTED} b.md")
    monkeypatch.setenv("BASE_REF", "main")
    _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        bundle.Bundle().refuse_unmergeable_paths()


def test_an_ordinary_text_path_is_not_unmergeable(step):
    step.refuse_unmergeable_paths()


# --- the two out-of-tree resolution channels ---------------------------------


# Any commit-shaped value: the head a refusal marks, so a test asserting the mark
# was NOT written is not just watching mark-handoff.sh refuse an unset HEAD_SHA.
_HEAD_SHA = "0" * 40


def _with_second_path(tmp_path, monkeypatch, main_extra=None, **env) -> "bundle.Bundle":
    _enter_repo(
        _repo(tmp_path, extra={"b.md": "base b\n"}, main_extra=main_extra), monkeypatch
    )
    monkeypatch.setenv("PR", "1")
    # The resolve job sets this for every step; the status comment builds its endpoint
    # from it.
    monkeypatch.setenv("GH_REPO", "owner/repo")
    monkeypatch.setenv("BUNDLE_DIR", str(tmp_path / "bundle"))
    monkeypatch.setenv("CONFLICT_LIST", f"{CONFLICTED} b.md")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _stub_gh(tmp_path, monkeypatch)
    return bundle.Bundle()


@pytest.mark.parametrize("decision", ["keep", "delete"], ids=["keep", "delete"])
def test_a_modify_delete_path_is_staged_from_its_verdict(
    tmp_path, monkeypatch, decision
):
    """The tree cannot express the answer: git leaves the surviving side's content
    there in BOTH directions, so a `git add` would resurrect a file the resolver
    judged should go."""
    verdicts = tmp_path / "verdicts.json"
    verdicts.write_text(json.dumps({"b.md": {"decision": decision}}), encoding="utf-8")
    step = _with_second_path(
        tmp_path,
        monkeypatch,
        MODIFY_DELETE_PATHS="b.md",
        MODIFY_DELETE_VERDICTS=str(verdicts),
    )
    step.stage_modify_delete()
    assert Path("b.md").exists() is (decision == "keep")


@pytest.mark.parametrize(
    "verdict",
    ['{"b.md": {"decision": "maybe"}}', "{}", "not json", '{"b.md": "keep"}'],
    ids=["undecided", "absent_entry", "corrupt", "not_an_object"],
)
def test_an_undecided_modify_delete_is_refused(tmp_path, monkeypatch, verdict):
    """An undecided modify/delete has no safe fallback — keep resurrects, delete
    removes — and both mistakes are invisible downstream."""
    verdicts = tmp_path / "verdicts.json"
    verdicts.write_text(verdict, encoding="utf-8")
    step = _with_second_path(
        tmp_path,
        monkeypatch,
        MODIFY_DELETE_PATHS="b.md",
        MODIFY_DELETE_VERDICTS=str(verdicts),
    )
    with pytest.raises(SystemExit):
        step.stage_modify_delete()


def test_a_declined_modify_delete_quotes_the_models_reasoning(tmp_path, monkeypatch):
    """A shard that read a modify/delete conflict and refused to decide has JUDGED
    it. Reporting that as "no verdict at all" sent a human to a file the model had
    already looked at, with nothing about why it would not choose."""
    verdicts = tmp_path / "verdicts.json"
    verdicts.write_text(
        json.dumps(
            {
                "b.md": {
                    "decision": "decline",
                    "reasoning": "the delete has no commit message behind it",
                }
            }
        ),
        encoding="utf-8",
    )
    step = _with_second_path(
        tmp_path,
        monkeypatch,
        MODIFY_DELETE_PATHS="b.md",
        MODIFY_DELETE_VERDICTS=str(verdicts),
    )
    with pytest.raises(SystemExit):
        step.stage_modify_delete()
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "the delete has no commit message behind it" in comment
    assert "no verdict for it at all" not in comment


def test_a_modify_delete_with_no_verdict_at_all_is_a_resolver_fault(
    tmp_path, monkeypatch
):
    """The decline record is what separates the model's refusal from the harness
    falling short, so an EMPTY verdict is now the harness — and a harness fault
    takes no handoff mark, because the fix lands outside this pull request and a
    re-run against the same head then answers differently."""
    verdicts = tmp_path / "verdicts.json"
    verdicts.write_text("{}", encoding="utf-8")
    step = _with_second_path(
        tmp_path,
        monkeypatch,
        MODIFY_DELETE_PATHS="b.md",
        MODIFY_DELETE_VERDICTS=str(verdicts),
        HEAD_SHA=_HEAD_SHA,
    )
    with pytest.raises(SystemExit):
        step.stage_modify_delete()
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "not even a decline" in log
    assert "auto-resolve/handed-off" not in log


@pytest.mark.parametrize(
    "path", ["", "/nonexistent/verdicts.json"], ids=["unset", "absent"]
)
def test_a_missing_verdict_file_is_named_as_plumbing(tmp_path, monkeypatch, path):
    step = _with_second_path(
        tmp_path, monkeypatch, MODIFY_DELETE_PATHS="b.md", MODIFY_DELETE_VERDICTS=path
    )
    with pytest.raises(SystemExit):
        step.stage_modify_delete()
    assert "plumbing" in (tmp_path / "gh.log").read_text(encoding="utf-8")


def test_no_modify_delete_paths_reads_no_verdict_file(step):
    step.stage_modify_delete()


def test_a_sidecar_resolution_is_installed_into_the_tree(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "b.resolved").write_text(
        "installed from the sidecar\n", encoding="utf-8"
    )
    resolutions = outside / "resolutions.json"
    resolutions.write_text(
        json.dumps({"b.md": str(outside / "b.resolved")}), encoding="utf-8"
    )
    step = _with_second_path(
        tmp_path,
        monkeypatch,
        SIDECAR_PATHS="b.md",
        SIDECAR_RESOLUTIONS=str(resolutions),
    )
    step.install_sidecar_resolutions()
    assert Path("b.md").read_text(encoding="utf-8") == "installed from the sidecar\n"


@pytest.mark.parametrize(
    "resolutions",
    ["{}", '{"b.md": ""}', '{"b.md": "/nonexistent/x"}', "not json"],
    ids=["absent_entry", "empty_path", "missing_file", "corrupt"],
)
def test_a_missing_sidecar_resolution_is_refused(tmp_path, monkeypatch, resolutions):
    """The path is still at git's conflicted content, so staging it would commit
    whichever side git left there — a silent half-merge."""
    outside = tmp_path / "outside"
    outside.mkdir()
    handle = outside / "resolutions.json"
    handle.write_text(resolutions, encoding="utf-8")
    step = _with_second_path(
        tmp_path, monkeypatch, SIDECAR_PATHS="b.md", SIDECAR_RESOLUTIONS=str(handle)
    )
    with pytest.raises(SystemExit):
        step.install_sidecar_resolutions()


def test_a_declined_sidecar_quotes_its_reasoning_instead_of_blaming_the_resolver(
    tmp_path, monkeypatch
):
    """A sidecar shard declines by handing out nothing, so absence alone cannot
    tell a judgement from a fault. The decline record can, and its reasoning is
    the whole value of the handoff to the human who now owns the merge."""
    outside = tmp_path / "outside"
    outside.mkdir()
    handle = outside / "resolutions.json"
    handle.write_text("{}", encoding="utf-8")
    step = _with_second_path(
        tmp_path,
        monkeypatch,
        SIDECAR_PATHS="b.md",
        SIDECAR_RESOLUTIONS=str(handle),
        HEAD_SHA=_HEAD_SHA,
    )
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {
                "file": "b.md",
                "resolved": False,
                "is_error": 0,
                "declined": True,
                "decline_reason": "both sides rewrote the same allowlist",
            }
        ],
    )
    with pytest.raises(SystemExit):
        step.install_sidecar_resolutions()
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "both sides rewrote the same allowlist" in log
    assert "recorded no decline" not in log
    assert "auto-resolve/handed-off" in log


def test_a_sidecar_that_handed_out_nothing_and_declined_nothing_is_a_resolver_fault(
    tmp_path, monkeypatch
):
    """No resolution and no decline record says nothing about whether the model
    judged the conflict or the harness fell over, and that ambiguity is what this
    tree exists to remove. It takes no handoff mark: the mark would strand the
    head until a human pushed, while the fix lands outside this pull request."""
    outside = tmp_path / "outside"
    outside.mkdir()
    handle = outside / "resolutions.json"
    handle.write_text("{}", encoding="utf-8")
    step = _with_second_path(
        tmp_path,
        monkeypatch,
        SIDECAR_PATHS="b.md",
        SIDECAR_RESOLUTIONS=str(handle),
        HEAD_SHA=_HEAD_SHA,
    )
    _execution_log(tmp_path, monkeypatch, [])
    with pytest.raises(SystemExit):
        step.install_sidecar_resolutions()
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "recorded no decline" in log
    assert "auto-resolve/handed-off" not in log


def test_a_symlinked_sidecar_resolution_is_refused_not_followed(tmp_path, monkeypatch):
    """A symlink planted at the scratch path is what turns a resolution channel
    into a copy-anything-into-the-repo primitive."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("not the merged content\n", encoding="utf-8")
    (outside / "b.resolved").symlink_to(outside / "secret")
    handle = outside / "resolutions.json"
    handle.write_text(
        json.dumps({"b.md": str(outside / "b.resolved")}), encoding="utf-8"
    )
    step = _with_second_path(
        tmp_path, monkeypatch, SIDECAR_PATHS="b.md", SIDECAR_RESOLUTIONS=str(handle)
    )
    with pytest.raises(SystemExit):
        step.install_sidecar_resolutions()
    assert "not the merged content" not in Path("b.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ["", "/nonexistent/r.json"], ids=["unset", "absent"])
def test_a_missing_sidecar_file_is_named_as_plumbing(tmp_path, monkeypatch, path):
    step = _with_second_path(
        tmp_path, monkeypatch, SIDECAR_PATHS="b.md", SIDECAR_RESOLUTIONS=path
    )
    with pytest.raises(SystemExit):
        step.install_sidecar_resolutions()
    assert "plumbing" in (tmp_path / "gh.log").read_text(encoding="utf-8")


def test_no_sidecar_paths_reads_no_resolution_file(step):
    step.install_sidecar_resolutions()


# --- staging, and the marker sweep -------------------------------------------


def test_the_text_resolutions_are_staged_and_the_decided_ones_are_not(
    tmp_path, monkeypatch
):
    """Re-adding a path the verdict deleted would undo that decision."""
    step = _with_second_path(tmp_path, monkeypatch, MODIFY_DELETE_PATHS="b.md")
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    step.stage_text_resolutions()
    assert step.staged == [CONFLICTED]


def test_an_empty_conflict_list_stages_nothing(tmp_path, monkeypatch):
    _enter_repo(_repo(tmp_path), monkeypatch)
    monkeypatch.setenv("PR", "1")
    # The resolve job sets this for every step; the status comment builds its endpoint
    # from it.
    monkeypatch.setenv("GH_REPO", "owner/repo")
    monkeypatch.setenv("BUNDLE_DIR", str(tmp_path / "bundle"))
    monkeypatch.setenv("CONFLICT_LIST", "")
    step = bundle.Bundle()
    step.stage_text_resolutions()
    assert step.staged == []


def test_a_marker_free_tree_passes_the_sweep(step):
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.marker_verdict().refuse_leftover_markers(".")


def test_leftover_markers_refuse_with_no_denials_reported(step, tmp_path, capsys):
    with pytest.raises(SystemExit):
        step.marker_verdict().refuse_leftover_markers(".")
    assert "Conflict markers still present:" in capsys.readouterr().out
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "left conflict markers behind" in comment


def _repo_with_two_conflicts(tmp_path: Path) -> Path:
    """Like `_repo`, but `b.md` ALSO conflicts, so one file can resolve cleanly
    while the other keeps its markers — the state a partial salvage answers."""
    work = tmp_path / "work"
    work.mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "commit.gpgsign", "false")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@e")
    for name, body in {
        CONFLICTED: "base a\n",
        "b.md": "base b\n",
        ".pre-commit-config.yaml": PRECOMMIT_FIXTURE,
    }.items():
        (work / name).write_text(body, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "checkout", "-q", "-b", "feature")
    (work / CONFLICTED).write_text("feature a\n", encoding="utf-8")
    (work / "b.md").write_text("feature b\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "feature")
    _git(work, "checkout", "-q", "main")
    (work / CONFLICTED).write_text("main a\n", encoding="utf-8")
    (work / "b.md").write_text("main b\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "main change")
    _git(work, "update-ref", "refs/remotes/origin/main", "main")
    _git(work, "checkout", "-q", "feature")
    subprocess.run(
        ["git", "-C", str(work), "merge", "--no-edit", "main"],
        capture_output=True,
        check=False,
    )
    return work


def _two_conflict_step(tmp_path, monkeypatch) -> "bundle.Bundle":
    """A Bundle wired to `_repo_with_two_conflicts`, tracking both paths — the
    two-file counterpart to the `step` fixture's single-conflict repo."""
    return _bundle_step(
        tmp_path, monkeypatch, _repo_with_two_conflicts(tmp_path), f"{CONFLICTED} b.md"
    )


def test_a_partial_refusal_salvages_the_files_that_did_resolve(tmp_path, monkeypatch):
    """A leftover-markers refusal with one file cleanly resolved and one still
    marked writes a patch for the resolved file, so a human resumes from 1 of 2
    instead of zero."""
    step = _two_conflict_step(tmp_path, monkeypatch)
    step.read_parents()
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    with pytest.raises(SystemExit):
        step.marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "1 of 2 conflicted file(s) resolved cleanly" in comment
    assert "auto-resolve-merge-1` artifact" in comment
    patch = (tmp_path / "bundle" / "salvage.patch").read_text(encoding="utf-8")
    assert "merged" in patch
    assert CONFLICTED in patch


def test_write_salvage_patch_skips_a_resolution_identical_to_the_merge_base(
    tmp_path, monkeypatch
):
    """A resolved file whose content already matches the merge base has nothing
    to diff — the patch is skipped rather than written empty."""
    step = _two_conflict_step(tmp_path, monkeypatch)
    step.read_parents()
    (Path.cwd() / CONFLICTED).write_text("base a\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    resolved, salvaged = step.marker_verdict().write_salvage_patch()
    assert resolved == [CONFLICTED]
    assert salvaged is False
    assert not (tmp_path / "bundle" / "salvage.patch").exists()


def test_still_marked_is_empty_over_a_tree_with_no_conflict_markers(step):
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    assert step.marker_verdict().still_marked() == set()


def _execution_log(tmp_path, monkeypatch, shards: list[dict]) -> None:
    """The fan-out's aggregate, where bundle reads which shards delivered."""
    fanout_dir = tmp_path / "fanout"
    fanout_dir.mkdir(exist_ok=True)
    (fanout_dir / "execution.json").write_text(
        json.dumps({"shards": shards}), encoding="utf-8"
    )
    monkeypatch.setenv("FANOUT_DIR", str(fanout_dir))


def test_a_shard_that_delivered_nothing_is_not_reported_as_a_hard_conflict(
    step, tmp_path, monkeypatch, capsys
):
    """A model that DECLINED the merge is a conflict for a human; a shard that ran
    and wrote no file is the resolver falling short. Saying the first when the
    second happened sends a human to read markers nobody judged."""
    _execution_log(
        tmp_path, monkeypatch, [{"file": CONFLICTED, "resolved": False, "is_error": 0}]
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "wrote no marker-free file" in comment
    assert "left conflict markers behind" not in comment
    assert CONFLICTED in capsys.readouterr().out


def _carry(tmp_path, monkeypatch, round_number: int, paths: list[str]) -> None:
    """The salvage an EARLIER round handed this run, as reuse-bundle stages it."""
    carried = tmp_path / "carried"
    carried.mkdir(exist_ok=True)
    (carried / "salvage.json").write_text(
        json.dumps({"round": round_number, "paths": paths}), encoding="utf-8"
    )
    monkeypatch.setenv("SALVAGE_DIR", str(carried))


def _starved_run(tmp_path, monkeypatch) -> dict[str, str]:
    """A wall-clock refusal that resolved one path of two, and the step outputs
    the workflow's continuation gate reads back from it."""
    outputs = tmp_path / "step-output"
    outputs.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    step = _with_second_path(tmp_path, monkeypatch, BASE_REF="main")
    _execution_log(
        tmp_path,
        monkeypatch,
        [{"file": CONFLICTED, "resolved": False, "is_error": 1, "timed_out": True}],
    )
    step.read_parents()
    # b.md resolved, a.md still marked because its shard never got the clock.
    (Path.cwd() / "b.md").write_text("merged\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        step.marker_verdict().refuse_leftover_markers(".")
    return dict(
        line.split("=", 1)
        for line in outputs.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


def test_a_starved_round_that_made_progress_earns_the_next_one(
    tmp_path, monkeypatch, capsys
):
    """The convergence gate. A window that ran out with part of the set resolved
    is the ONE ending another window fixes, so this refusal asks for another
    round — and the salvage it just wrote is what that round starts from."""
    outputs = _starved_run(tmp_path, monkeypatch)
    assert outputs["carry_continue"] == "true"
    assert outputs["carry_round"] == "1"
    capsys.readouterr()


def test_a_round_that_resolved_no_more_than_it_carried_ends_the_chain(
    tmp_path, monkeypatch, capsys
):
    """Progress is the whole licence to spend again. A round that installed two
    paths and resolved one has not shrunk the set, so another round buys the
    same wall."""
    _carry(tmp_path, monkeypatch, 1, ["one.md", "two.md"])
    outputs = _starved_run(tmp_path, monkeypatch)
    assert outputs["carry_continue"] == ""
    assert outputs["carry_round"] == "2"
    capsys.readouterr()


def test_the_chain_stops_at_its_cap_however_much_progress_it_makes(
    tmp_path, monkeypatch, capsys
):
    """The cap is what bounds a conflict set that shrinks by one path a round."""
    _carry(tmp_path, monkeypatch, 9, [])
    outputs = _starved_run(tmp_path, monkeypatch)
    assert outputs["carry_continue"] == ""
    assert outputs["carry_round"] == "10"
    capsys.readouterr()


def test_a_chain_still_shrinking_the_set_keeps_going_past_a_few_rounds(
    tmp_path, monkeypatch, capsys
):
    """One round reaches about eight files, so a set of twenty needs more than a
    handful of them. A cap near one round's width would refuse exactly the sets
    the carry exists for, while the progress test still ends a stalled chain."""
    _carry(tmp_path, monkeypatch, 4, [])
    outputs = _starved_run(tmp_path, monkeypatch)
    assert outputs["carry_continue"] == "true"
    assert outputs["carry_round"] == "5"
    capsys.readouterr()


def test_a_refusal_the_clock_did_not_cause_earns_no_further_round(
    tmp_path, monkeypatch, capsys
):
    """A decline and a denied grant both reproduce exactly, so another window
    buys the identical refusal. Only the wall clock earns a second run."""
    outputs = tmp_path / "step-output"
    outputs.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    step = _declined_fixture(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        step.marker_verdict().refuse_leftover_markers(CONFLICTED, "b.md")
    written = dict(
        line.split("=", 1)
        for line in outputs.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert written["carry_continue"] == ""
    capsys.readouterr()


def test_a_shard_the_WALL_CLOCK_killed_is_not_reported_as_the_models_verdict(
    step, tmp_path, monkeypatch, capsys
):
    """PR 4340 stood still at 8 of 23 files for three days. Its fan-out spent
    FANOUT_BUDGET_SECONDS and the remaining shards never ran, and this refusal
    published that truncation as the model's own verdict — which discover holds
    THROUGH a resolver change, so the very fix that gives the fan-out room could
    not re-open the pull request."""
    _execution_log(
        tmp_path,
        monkeypatch,
        [{"file": CONFLICTED, "resolved": False, "is_error": 1, "timed_out": True}],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    # One starved shard is fewer than this run's reachable capacity, so the
    # wall-clock branch names the single exhausted shard rather than the set.
    assert "exhausted `SHARD_TIMEOUT_SECONDS`" in comment
    assert "nothing here is a judgement that the conflict is too hard" in comment
    assert "wrote no marker-free file" not in comment
    # The mark this run leaves is what decides whether the resolver fix reaches
    # this head, so the test reads both contexts from the file both sides read.
    assert f"context={_MARKS['auto_resolve_handoff']}" in comment
    assert _MARKS["auto_resolve_declined"] not in comment
    capsys.readouterr()


def test_one_block_answering_does_not_answer_for_a_block_the_clock_killed(
    step, tmp_path, monkeypatch, capsys
):
    """The case a sibling block hides. Block A declines, block B is killed at
    SHARD_TIMEOUT_SECONDS, and B's markers stay in the tree. `unanswered_files`
    drops the file for B's error and the residue pass gives it no retry, so
    reading A's answer as the file's answer sends the whole file to the decline
    mark — for a hunk no model read."""
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 0,
                "whole_file": False,
                "declined": True,
                "decline_reason": "both sides rewrote the same guard",
            },
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 1,
                "whole_file": False,
                "timed_out": True,
            },
        ],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "nothing here is a judgement that the conflict is too hard" in comment
    assert f"context={_MARKS['auto_resolve_handoff']}" in comment
    assert _MARKS["auto_resolve_declined"] not in comment
    capsys.readouterr()


def test_a_file_another_shard_answered_is_not_starved_by_one_timed_out_block(
    step, tmp_path, monkeypatch, capsys
):
    """A file cut into blocks keeps one shard per block, so a block that ran out
    of clock after another block's answer covered the file must not turn the
    file's verdict into the wall clock and hide the reasoning the model gave."""
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 1,
                "timed_out": True,
                "whole_file": False,
            },
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 0,
                "whole_file": True,
                "declined": True,
                "decline_reason": "both sides rewrote the same guard",
            },
        ],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "ran out of wall clock" not in comment
    assert "both sides rewrote the same guard" in comment
    capsys.readouterr()


def test_a_shard_that_DECLINED_is_reported_as_its_judgement_with_its_reasoning(
    step, tmp_path, monkeypatch, capsys
):
    """PR 4340's own refusal comment said the opposite of the truth for two days:
    its shard had judged the conflict, and the comment called that judgement a
    resolver defect with nothing about which block or why. The decline RECORD is
    what separates the two causes, and its reasoning is the whole value of the
    handoff to the human who now owns the merge."""
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 0,
                "declined": True,
                "decline_reason": "both sides rewrote the same guard",
            }
        ],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "wrote no marker-free file" not in comment
    assert "both sides rewrote the same guard" in comment
    capsys.readouterr()


def test_a_DECLINED_conflict_hands_over_a_prompt_the_reader_can_paste(
    step, tmp_path, monkeypatch, capsys
):
    """A refusal that names a decision nobody made leaves the reader rebuilding
    the context this run already holds. The escalation block carries the
    branches, the paths and the resolver's own account into whichever model the
    reader asks, so they answer the question the resolver asked."""
    monkeypatch.setenv("HEAD_REF", "claude/widen-the-guard")
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 0,
                "declined": True,
                "decline_reason": "both sides rewrote the same guard",
            }
        ],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "needs a higher-level decision" in comment
    # Everything the reader would otherwise retype: both branches, the repo and
    # PR, the path, and what the resolver would not decide.
    for carried in (
        "claude/widen-the-guard",
        "main",
        "owner/repo",
        CONFLICTED,
        "both sides rewrote the same guard",
    ):
        assert carried in comment
    capsys.readouterr()


def test_the_handover_prompt_tells_the_next_session_to_decide_not_to_ask(
    step, tmp_path, monkeypatch, capsys
):
    """The reader pastes the block into a fresh session and walks away, so the
    session it reaches has nobody to question. A prompt that asks for the intent
    behind a side ends that session with the conflict still standing."""
    monkeypatch.setenv("HEAD_REF", "claude/widen-the-guard")
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 0,
                "declined": True,
                "decline_reason": "both sides rewrote the same guard",
            }
        ],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "resolve it yourself" in comment
    # Combining both sides is the usual right answer, so the prompt must not read
    # as "pick a winner" — the declined conflicts are the ones where each side
    # carries something the merged file needs.
    assert "combine both sides" in comment
    # The block says to paste it into a chat holding the two file bodies, so the
    # decision record lands in that session's ANSWER; the repository is what the
    # pull-request write and the test run are gated on.
    assert "State the choice and its alternative in your answer" in comment
    assert "If you have the repository" in comment
    for asked in ("ask me", "put it to me", "before you propose a resolution"):
        assert asked not in comment
    capsys.readouterr()


def test_leftover_markers_with_no_decline_record_hand_over_no_prompt(
    step, tmp_path, monkeypatch, capsys
):
    """Reaching the last branch is not evidence of a judgement. A generated file
    its generator did not re-derive keeps its markers and records no decline, and
    a prompt calling both sides defensible would send the reader to argue with a
    model about a build step."""
    # The shape the corpus scenario has: the marked path carries no shard at all,
    # so nothing judged it and nothing failed on it either.
    _execution_log(
        tmp_path, monkeypatch, [{"file": "b.md", "resolved": True, "is_error": 0}]
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "left conflict markers behind" in comment
    assert "needs a higher-level decision" not in comment
    # The trailer names the still-conflicted hunk and says plainly that no
    # shard recorded a reason for it — distinct from a shard the harness
    # reports FAILED, which never reaches this branch at all.
    assert f"`{CONFLICTED}` (lines 1-5): the shard recorded no reason" in comment
    capsys.readouterr()


def test_a_refusal_with_a_REMEDY_hands_over_no_prompt(
    step, tmp_path, monkeypatch, capsys
):
    """The wall clock has a remedy, not a judgement: nobody decides anything, so
    a prompt asking the reader to weigh both sides would send them to argue with
    a model about hunks no model read."""
    _execution_log(
        tmp_path,
        monkeypatch,
        [{"file": CONFLICTED, "resolved": False, "is_error": 1, "timed_out": True}],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "nothing here is a judgement that the conflict is too hard" in comment
    assert "needs a higher-level decision" not in comment
    capsys.readouterr()


def test_a_DECLINE_with_no_reasoning_is_still_the_models_verdict(
    step, tmp_path, monkeypatch, capsys
):
    """A shard that recorded `decline` and no sentence still DECIDED, so the
    refusal must not fall back to blaming the resolver — every harness cause is
    ruled out before this branch is reached."""
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 0,
                "declined": True,
                "decline_reason": "",
            }
        ],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "wrote no marker-free file" not in comment
    assert "left conflict markers behind" in comment
    assert "own account of what it would not merge" not in comment
    capsys.readouterr()


def test_a_finished_residue_retry_clears_its_own_block_shards_silence(
    step, tmp_path, monkeypatch, capsys
):
    """The whole-file shard answers FOR the file. A file cut into blocks keeps its
    original block shard's `resolved: false` after a residue retry finished the
    file, and judging each shard alone called that file a resolver fault — the
    exact wrong diagnosis this change removes, arriving one layer down."""
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 0,
                "whole_file": False,
            },
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 0,
                "whole_file": True,
                "declined": True,
                "decline_reason": "the two sides moved the same block",
            },
        ],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "wrote no marker-free file" not in comment
    assert "the two sides moved the same block" in comment
    capsys.readouterr()


def test_a_decline_reasoning_is_truncated_before_it_reaches_the_comment(
    step, tmp_path, monkeypatch, capsys
):
    """A shard writes its reasoning after reading the conflicted file, so the PR
    branch's own content influences it — and this is the only path carrying
    free-form model text into the sticky comment. Unbounded, it could also push
    the comment past what `gh` will post, which would cost the refusal itself."""
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 0,
                "declined": True,
                "decline_reason": "opening claim. " + "padding " * 400 + "TAIL-MARKER",
            }
        ],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "opening claim." in comment
    assert "TAIL-MARKER" not in comment
    capsys.readouterr()


# --- naming the hunk and the shard's reason, per still-conflicted path -------


def test_a_declined_hunk_is_named_by_its_line_range_and_the_models_reason(
    step, tmp_path, monkeypatch, capsys
):
    """agent-glovebox#5556: a refusal that only named the FILE sent a human to
    scan a 1925-line file for the region a shard judged. The trailer now names
    the hunk's own line range and the model's account of it."""
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 0,
                "declined": True,
                "decline_reason": "both sides rewrote the same guard",
            }
        ],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    # The `step` fixture's single-line conflict merges to a bare 5-line hunk
    # with no context before it (see CONFLICTED_BODIES): lines 1-5.
    assert f"`{CONFLICTED}` (lines 1-5): both sides rewrote the same guard" in comment
    capsys.readouterr()


def test_a_declined_hunks_line_range_reflects_its_real_offset_in_the_file(
    tmp_path, monkeypatch, capsys
):
    """The range is read off the CURRENT file, not assumed to start at line 1 —
    a conflict preceded by unchanged context lands its hunk further down."""
    step = _bundle_step(
        tmp_path,
        monkeypatch,
        _repo(
            tmp_path,
            bodies=(
                "top\nbase\nbottom\n",
                "top\nfeature side\nbottom\n",
                "top\nmain side\nbottom\n",
            ),
        ),
        CONFLICTED,
    )
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 0,
                "declined": True,
                "decline_reason": "both sides rewrote the same guard",
            }
        ],
    )
    with pytest.raises(SystemExit):
        step.marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert f"`{CONFLICTED}` (lines 2-6): both sides rewrote the same guard" in comment
    capsys.readouterr()


def test_a_declined_reasons_comment_trailer_is_truncated_separately(
    step, tmp_path, monkeypatch, capsys
):
    """The aggregate sentence (`_decline_reasons`, bounded at 1024 chars) and
    the per-path trailer (bounded at `_COMMENT_REASON_CHARS`) truncate
    INDEPENDENTLY: a reasoning short enough for the first still overruns the
    second, whose own bound applies to every path's line, not one sentence."""
    long_reason = "opening. " + "x" * 250
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {
                "file": CONFLICTED,
                "resolved": False,
                "is_error": 0,
                "declined": True,
                "decline_reason": long_reason,
            }
        ],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    # The aggregate sentence quotes the reason whole (it stays under 1024
    # chars), so the trailer's own truncated form is what proves the SEPARATE,
    # tighter bound: the bullet line itself never carries the untruncated tail.
    truncated = long_reason[: marker_verdict._COMMENT_REASON_CHARS]
    assert truncated != long_reason  # the fixture must actually exceed the bound
    assert f"- `{CONFLICTED}` (lines 1-5): {truncated}…" in comment
    capsys.readouterr()


# --- telling one oversized hunk apart from a conflict set past capacity -----


def test_one_shard_that_exhausted_its_own_timeout_blames_the_hunk_not_the_set(
    step, tmp_path, monkeypatch, capsys
):
    """agent-glovebox#5508: a single ~2500-line hunk sent every reader to raise
    `MAX_PARALLEL`, which changes nothing when only one shard ever ran. Fewer
    starved shards than this run's reachable capacity means the budget was not
    the binding constraint — one shard alone ran past `SHARD_TIMEOUT_SECONDS`."""
    for name in ("SHARD_TIMEOUT_SECONDS", "FANOUT_BUDGET_SECONDS", "MAX_PARALLEL"):
        monkeypatch.delenv(name, raising=False)
    _execution_log(
        tmp_path,
        monkeypatch,
        [{"file": CONFLICTED, "resolved": False, "is_error": 1, "timed_out": True}],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "exhausted `SHARD_TIMEOUT_SECONDS`" in comment
    assert f"`{CONFLICTED}` lines 1-5 (5 lines)" in comment
    assert "MAX_PARALLEL` buys nothing here" in comment
    assert "conflict set past that size" not in comment
    capsys.readouterr()


def test_a_starved_set_past_reachable_capacity_keeps_the_set_size_message(
    step, tmp_path, monkeypatch, capsys
):
    """The set-size diagnosis stays for the case it actually describes: as many
    starved shards as this run could ever have carried in one window, so more
    parallelism or budget genuinely would have helped."""
    monkeypatch.setenv("MAX_PARALLEL", "1")
    monkeypatch.setenv("SHARD_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("FANOUT_BUDGET_SECONDS", "600")
    _execution_log(
        tmp_path,
        monkeypatch,
        [{"file": CONFLICTED, "resolved": False, "is_error": 1, "timed_out": True}],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    comment = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "conflict set past that size" in comment
    assert "exhausted `SHARD_TIMEOUT_SECONDS` before it resolved" not in comment
    assert "MAX_PARALLEL` buys nothing here" not in comment
    capsys.readouterr()


def test_a_file_with_BOTH_an_errored_and_an_undelivered_shard_is_not_no_deliverable(
    step, tmp_path, monkeypatch, capsys
):
    """A multi-block file where one block times out while another declines has
    a real execution error, not a clean "ran and wrote nothing" — calling it the
    latter tells a human a false thing about the one claim this function exists
    to establish, and hides the credential-ladder-worthy error underneath it."""
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {"file": CONFLICTED, "resolved": False, "is_error": 1},
            {"file": CONFLICTED, "resolved": False, "is_error": 0},
        ],
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    assert "wrote no marker-free file" not in capsys.readouterr().out


def test_a_shard_that_ERRORED_keeps_the_ordinary_verdict(
    step, tmp_path, monkeypatch, capsys
):
    """A shard whose process failed left no deliverable either, so a diagnosis
    keyed on 'no file' alone would claim a clean run for a crashed one."""
    _execution_log(
        tmp_path, monkeypatch, [{"file": CONFLICTED, "resolved": False, "is_error": 1}]
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    assert "wrote no marker-free file" not in capsys.readouterr().out


def test_an_execution_log_of_the_WRONG_SHAPE_still_publishes_the_refusal(
    step, tmp_path, monkeypatch, capsys
):
    """Readable JSON that is not the aggregate — a truncated write, a shape
    change — must fall the same way an absent one does, not crash the refusal
    the merge is being aborted over."""
    fanout_dir = tmp_path / "fanout"
    fanout_dir.mkdir()
    (fanout_dir / "execution.json").write_text('{"shards": "none"}', encoding="utf-8")
    monkeypatch.setenv("FANOUT_DIR", str(fanout_dir))
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    assert "left conflict markers behind" in (tmp_path / "gh.log").read_text(
        encoding="utf-8"
    )
    assert "wrote no marker-free file" not in capsys.readouterr().out


def test_an_unreadable_execution_log_still_publishes_the_refusal(
    step, tmp_path, monkeypatch, capsys
):
    """The log only SHARPENS the verdict, so it must never be the reason a
    refusal cannot be published — the merge is aborted either way."""
    monkeypatch.setenv("FANOUT_DIR", str(tmp_path / "absent"))
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    assert "left conflict markers behind" in (tmp_path / "gh.log").read_text(
        encoding="utf-8"
    )
    assert "wrote no marker-free file" not in capsys.readouterr().out


def test_another_files_undelivered_shard_does_not_claim_this_conflict(
    step, tmp_path, monkeypatch, capsys
):
    """The verdict must accuse only the files it has evidence about: a shard that
    delivered nothing for a path with no leftover markers says nothing about the
    path that does have them."""
    _execution_log(
        tmp_path, monkeypatch, [{"file": "other.md", "resolved": False, "is_error": 0}]
    )
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    assert "wrote no marker-free file" not in capsys.readouterr().out


def test_unnamed_denials_claim_neither_cause(step, monkeypatch, capsys):
    """Neither cause is established: name that, rather than picking one."""
    monkeypatch.setenv("LLM_PERMISSION_DENIALS", "2")
    step = bundle.Bundle()
    with pytest.raises(SystemExit):
        step.marker_verdict().refuse_leftover_markers(".")
    assert "did not name" in capsys.readouterr().out


def test_a_denied_edit_tool_labels_the_pull_request(step, monkeypatch, tmp_path):
    """A closed write path is a property of the resolver's own configuration, not
    of this conflict: the next base push re-runs the identical denial."""
    monkeypatch.setenv("LLM_PERMISSION_DENIALS", "1")
    monkeypatch.setenv("LLM_PERMISSION_DENIED_TOOLS", '["Edit"]')
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    calls = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "auto-resolve-blocked" in calls


def test_non_edit_denials_are_named_as_not_the_cause(step, monkeypatch, capsys):
    monkeypatch.setenv("LLM_PERMISSION_DENIALS", "2")
    monkeypatch.setenv("LLM_PERMISSION_DENIED_TOOLS", '["Bash","TodoWrite"]')
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    assert "did not block the resolution" in capsys.readouterr().out


def test_a_denial_on_another_shard_keeps_auto_resolve_enabled(
    step, monkeypatch, tmp_path, capsys
):
    """Blaming the grants here would label the whole PR out of auto-resolve over a
    denial that cost this resolution nothing."""
    monkeypatch.setenv("LLM_PERMISSION_DENIALS", "1")
    monkeypatch.setenv("LLM_PERMISSION_DENIED_TOOLS", '["Edit"]')
    monkeypatch.setenv("LLM_PERMISSION_DENIALS_BY_FILE", '{"other.md":["Edit"]}')
    with pytest.raises(SystemExit):
        bundle.Bundle().marker_verdict().refuse_leftover_markers(".")
    assert "other files' shards" in capsys.readouterr().out
    assert "auto-resolve-blocked" not in (tmp_path / "gh.log").read_text(
        encoding="utf-8"
    )


def test_the_deferred_paths_are_excluded_from_the_pre_regeneration_sweep(
    tmp_path, monkeypatch
):
    """A generator handed `<<<<<<<` dies on a syntax error, so re-deriving first
    would make the crash the reported verdict and blame a derived artifact no
    human should hand-edit."""
    step = _with_second_path(tmp_path, monkeypatch, DEFERRED_REGEN="b.md")
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    (Path.cwd() / "b.md").write_text(
        "<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> main\n", encoding="utf-8"
    )
    git_io.git("add", "--", "b.md")
    step.marker_verdict().refuse_leftover_markers(".", ":(exclude)b.md")
    with pytest.raises(SystemExit):
        step.marker_verdict().refuse_leftover_markers(".")


# --- the deferred regeneration -----------------------------------------------


def _stub_pnpm(tmp_path, monkeypatch, body: str) -> None:
    """A `pnpm` on PATH, plus the pre-pass command that reaches it.

    This resolver holds no generator of its own: the calling repository names one
    through `AUTO_RESOLVE_PRE_PASS`, which the module reads once at import. So a
    stub binary alone would leave `PRE_PASS` empty and every case here would take
    the no-pre-pass refusal instead of the branch it names.
    """
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    stub = binaries / "pnpm"
    stub.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")
    monkeypatch.setattr(bundle._pre_pass, "PRE_PASS", ["pnpm", "resolve-generated"])


def test_no_deferred_paths_and_no_pre_pass_runs_nothing(step, monkeypatch):
    monkeypatch.setattr(bundle._pre_pass, "PRE_PASS", [])
    monkeypatch.setattr(
        bundle._pre_pass,
        "run_pre_pass",
        lambda *a: pytest.fail("no pre-pass command to run"),
    )
    step.run_deferred_regeneration()


def test_the_pre_pass_runs_even_with_nothing_deferred(step, tmp_path, monkeypatch):
    """A generated file whose SOURCES conflicted can text-merge cleanly itself, so
    it is deferred nowhere while holding bytes its generator no longer produces
    (agent-glovebox#5363). Only a re-derive over the staged resolution refreshes
    it, so the pre-pass runs whenever the caller declared one."""
    _stub_pnpm(tmp_path, monkeypatch, f'touch "{tmp_path}/ran"\nexit 0')
    step.run_deferred_regeneration()
    assert (tmp_path / "ran").exists()


def _hash_object(text: str) -> str:
    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        input=text,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _leave_unmerged(name: str, *, sides: tuple[str, str, str] | None = None) -> None:
    """Put `name` back in the index as an unresolved conflict — the state a
    regeneration rule that never fired leaves behind. The index, not the file
    content, is what the step reads, so writing markers into the file is not
    enough to reproduce it.

    SIDES gives the base, ours and theirs texts each stage holds; without it all
    three carry the work-tree file's own blob, which is the merge where every
    side already agrees."""
    if sides is None:
        blob = _hash_object(Path(name).read_text(encoding="utf-8"))
        blobs = (blob, blob, blob)
    else:
        blobs = tuple(_hash_object(text) for text in sides)
    stages = "".join(
        f"100644 {blob} {stage}\t{name}\n" for stage, blob in enumerate(blobs, 1)
    )
    subprocess.run(
        ["git", "update-index", "--force-remove", "--", name],
        check=True,
        cwd=Path.cwd(),
    )
    subprocess.run(
        ["git", "update-index", "--index-info"],
        input=stages,
        text=True,
        check=True,
        cwd=Path.cwd(),
    )


def test_a_deferred_path_with_no_pre_pass_command_is_refused(
    tmp_path, monkeypatch, capsys
):
    """This resolver knows no generator of its own. A caller that declared none and
    still has a deferred generated path gets a refusal, not a bundle holding
    whatever the model wrote into a file no build produces."""
    step = _with_second_path(tmp_path, monkeypatch, DEFERRED_REGEN="b.md")
    monkeypatch.setattr(bundle._pre_pass, "PRE_PASS", [])
    _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        step.run_deferred_regeneration()
    assert "deferred with no pre-pass command" in capsys.readouterr().out


def _stub_pnpm_verify(tmp_path, monkeypatch, verify_rc: int) -> None:
    """A pre-pass that writes nothing and answers `--verify` with VERIFY_RC — the
    idempotent generator, told whether the work tree is what it produces."""
    _stub_pnpm(
        tmp_path,
        monkeypatch,
        f'[[ " $* " == *" --verify "* ]] && exit {verify_rc}\nexit 0',
    )


# A merge where the two sides differ, so the derived output is neither of them —
# the shape a generated file conflicts in.
_SIDES = ("base b\n", "ours b\n", "theirs b\n")
_DERIVED = "derived from ours and theirs\n"


def _staged_text(name: str) -> str:
    return subprocess.run(
        ["git", "show", f":{name}"], capture_output=True, text=True, check=True
    ).stdout


def _unmerged_stages(name: str) -> str:
    return subprocess.run(
        ["git", "ls-files", "-u", "--", name],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_a_deferred_path_still_unmerged_is_refused(tmp_path, monkeypatch, capsys):
    """The generator wrote nothing AND says the bytes are not what it produces, so
    nothing here re-derived the file."""
    step = _with_second_path(tmp_path, monkeypatch, DEFERRED_REGEN="b.md")
    _stub_pnpm_verify(tmp_path, monkeypatch, 4)
    _stub_gh(tmp_path, monkeypatch)
    Path("b.md").write_text(_DERIVED, encoding="utf-8")
    _leave_unmerged("b.md", sides=_SIDES)
    with pytest.raises(SystemExit):
        step.run_deferred_regeneration()
    assert "did not regenerate cleanly" in capsys.readouterr().out


def test_a_deferred_path_the_pre_pass_already_made_current_is_staged(
    tmp_path, monkeypatch, capsys
):
    """A generator is idempotent, so prepare.sh's own pre-pass having already
    written the merged tree's output leaves this pass with nothing to write and
    nothing to stage (agent-sanitizer#396). The path keeps the index's conflict
    stages, and reading the index alone calls that a failure to regenerate."""
    step = _with_second_path(tmp_path, monkeypatch, DEFERRED_REGEN="b.md")
    _stub_pnpm_verify(tmp_path, monkeypatch, 0)
    _stub_gh(tmp_path, monkeypatch)
    Path("b.md").write_text(_DERIVED, encoding="utf-8")
    _leave_unmerged("b.md", sides=_SIDES)

    step.run_deferred_regeneration()

    assert _unmerged_stages("b.md") == ""
    # The staged blob is the pre-pass's output, not either parent's side.
    assert _staged_text("b.md") == _DERIVED
    assert "already current" in capsys.readouterr().out


@pytest.mark.parametrize("side", [_SIDES[1], _SIDES[2]], ids=["ours", "theirs"])
def test_a_deferred_path_left_at_a_parents_own_side_is_refused(
    tmp_path, monkeypatch, capsys, side
):
    """git leaves a binary, a `-merge` file and a modify/delete at one parent's
    side with no markers, and prepare.sh routes a generated-owned one into the
    deferred set before any other partition can claim it. A whole-tree `--verify`
    that never read the path still answers 0, so staging it would commit "ours" as
    the resolution."""
    step = _with_second_path(tmp_path, monkeypatch, DEFERRED_REGEN="b.md")
    _stub_pnpm_verify(tmp_path, monkeypatch, 0)
    _stub_gh(tmp_path, monkeypatch)
    Path("b.md").write_text(side, encoding="utf-8")
    _leave_unmerged("b.md", sides=_SIDES)

    with pytest.raises(SystemExit):
        step.run_deferred_regeneration()

    assert "did not regenerate cleanly" in capsys.readouterr().out


def test_the_generated_artifact_gate_re_reads_the_tree_on_every_call(
    tmp_path, monkeypatch
):
    """`repair_and_reverify` runs this gate again after a model pass rewrote the
    merged tree, so an answer carried from an earlier call would pass the repaired
    tree on evidence about the tree before it."""
    step = _with_second_path(tmp_path, monkeypatch)
    _stub_gh(tmp_path, monkeypatch)
    _stub_pnpm(
        tmp_path,
        monkeypatch,
        f'[[ -e "{tmp_path}/stale" ]] && exit 5\ntouch "{tmp_path}/stale"\nexit 0',
    )

    step.verify_generated_artifacts()
    with pytest.raises(SystemExit):
        step.verify_generated_artifacts()


def test_a_deferred_path_carrying_markers_is_refused_however_verify_answers(
    tmp_path, monkeypatch
):
    """Conflict text is what no generator produces, so the work tree cannot be a
    re-derivation of it — whatever a `--verify` that never read this path says."""
    step = _with_second_path(tmp_path, monkeypatch, DEFERRED_REGEN="b.md")
    _stub_pnpm_verify(tmp_path, monkeypatch, 0)
    _stub_gh(tmp_path, monkeypatch)
    Path("b.md").write_text(
        "<<<<<<< HEAD\nours b\n=======\ntheirs b\n>>>>>>> other\n", encoding="utf-8"
    )
    _leave_unmerged("b.md", sides=_SIDES)
    with pytest.raises(SystemExit):
        step.run_deferred_regeneration()


def test_a_non_zero_pre_pass_is_refused_even_when_every_path_came_back(
    tmp_path, monkeypatch, capsys
):
    """Some OTHER rule crashed, so a derived file in the tree may not match its
    merged sources."""
    step = _with_second_path(tmp_path, monkeypatch, DEFERRED_REGEN="b.md")
    _stub_pnpm(
        tmp_path,
        monkeypatch,
        'git add -- b.md\necho "MissingMeta: declare each directive" >&2\nexit 3',
    )
    log = _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        step.run_deferred_regeneration()
    assert "exited 3" in capsys.readouterr().out
    # The generator's own remedy reaches the handoff comment, not only the log
    # (agent-glovebox#5382): the published refusal carries the re-derive error.
    assert "MissingMeta: declare each directive" in log.read_text(encoding="utf-8")


def test_a_crashing_generator_gets_one_repair_pass_before_the_handoff(
    tmp_path, monkeypatch
):
    """A generator reads the merged SOURCES as a program, so it dies on a file git
    text-merged into something that does not run — a name one side renamed and the
    other still calls. The repair pass fixes that class, so the generator runs
    again before this hands the conflict to a human."""
    step = _with_second_path(tmp_path, monkeypatch, DEFERRED_REGEN="b.md")
    _stub_pnpm(
        tmp_path,
        monkeypatch,
        f'git add -- b.md\n[[ -e "{tmp_path}/repaired" ]] || {{ echo "NameError: _in"; exit 3; }}',
    )
    reports = []
    monkeypatch.setattr(
        type(step),
        "repair_merged_tree",
        lambda _self, report, _rejected_by: (
            reports.append(report.read_text(encoding="utf-8")),
            (tmp_path / "repaired").touch(),
            True,
        )[-1],
    )

    step.run_deferred_regeneration()

    assert "NameError: _in" in reports[0]


def test_a_clean_pre_pass_passes(tmp_path, monkeypatch):
    step = _with_second_path(tmp_path, monkeypatch, DEFERRED_REGEN="b.md")
    _stub_pnpm(tmp_path, monkeypatch, "git add -- b.md\nexit 0")
    step.run_deferred_regeneration()


def _pre_pass_binary_missing(tmp_path, monkeypatch) -> None:
    """A caller that names a pre-pass this job never installs — the shape #4586
    reported. `_stub_pnpm`'s opposite: the command is declared, the binary is not
    on PATH, so the interpreter raises before any child exists.
    """
    monkeypatch.setenv("PATH", path_without_binary("pnpm", tmp_path / "bin"))
    monkeypatch.setattr(bundle._pre_pass, "PRE_PASS", ["pnpm", "resolve-generated"])


def test_a_pre_pass_binary_the_runner_lacks_is_named_as_plumbing(
    step, tmp_path, monkeypatch, capsys
):
    """`check=False` catches a non-zero EXIT and nothing else, so an uninstalled
    pre-pass raises out of this step after the model has billed the whole
    resolution. Unhandled, that raise uploads no bundle and reports `gave_up`,
    which reads exactly like a merge the resolver could not do.

    It takes no handoff mark, so a re-run after the caller installs the tool
    resolves this same head instead of waiting out the mark's TTL."""
    _pre_pass_binary_missing(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        step.verify_generated_artifacts()
    assert "will not run on this runner" in capsys.readouterr().out
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "installs no such binary" in log
    assert "auto-resolve/handed-off" not in log


def test_the_same_missing_pre_pass_stops_the_deferred_re_derivation(
    tmp_path, monkeypatch, capsys
):
    """The other call site, which runs BEFORE the bundle is written: the same
    missing binary loses the same resolution one step earlier."""
    step = _with_second_path(tmp_path, monkeypatch, DEFERRED_REGEN="b.md")
    _pre_pass_binary_missing(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        step.run_deferred_regeneration()
    assert "will not run on this runner" in capsys.readouterr().out
    assert "auto-resolve/handed-off" not in (tmp_path / "gh.log").read_text(
        encoding="utf-8"
    )


def test_a_generated_artifact_that_does_not_match_is_refused(
    step, tmp_path, monkeypatch, capsys
):
    """A generated file that git text-merged CLEANLY answers "no" to every check
    above while holding bytes no build produces."""
    _stub_pnpm(tmp_path, monkeypatch, 'echo "lockfile differs"; exit 1')
    _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        step.verify_generated_artifacts()
    assert "lockfile differs" in capsys.readouterr().out


def test_a_matching_generated_artifact_passes(step, tmp_path, monkeypatch):
    _stub_pnpm(tmp_path, monkeypatch, "exit 0")
    step.verify_generated_artifacts()


def test_a_fork_head_runs_no_pre_pass_binary_at_all(step, monkeypatch):
    """The untrusted-head boundary, over this post-condition. `<pre-pass> --verify`
    runs a script the fork's own manifest defines, and the resolve job installs no
    package manager for such a run — so an inherited command does not skip the pass,
    it dies on a missing binary after the model billed the whole resolution.

    The module reads the command at IMPORT, so the case loads its own copy under the
    fork environment instead of patching the constant the fix derives."""
    monkeypatch.setenv("AUTO_RESOLVE_PRE_PASS", "pnpm resolve-generated")
    monkeypatch.setenv("AUTO_RESOLVE_UNTRUSTED_HEAD", "true")
    monkeypatch.setenv("PATH", path_without_binary("pnpm"))
    fork = load_script(".github/resolver/auto-resolve/bundle.py")
    fork.Bundle.verify_generated_artifacts(step)


def test_a_same_repo_head_still_verifies_the_generated_artifacts(
    step, tmp_path, monkeypatch, capsys
):
    """The other direction, so the skip cannot widen silently: anything but the exact
    string leaves the post-condition enforcing."""
    monkeypatch.setenv("AUTO_RESOLVE_PRE_PASS", "pnpm resolve-generated")
    monkeypatch.setenv("AUTO_RESOLVE_UNTRUSTED_HEAD", "false")
    _stub_pnpm(tmp_path, monkeypatch, 'echo "lockfile differs"; exit 1')
    same_repo = load_script(".github/resolver/auto-resolve/bundle.py")
    with pytest.raises(SystemExit):
        same_repo.Bundle.verify_generated_artifacts(step)
    assert "lockfile differs" in capsys.readouterr().out


# --- the caller's whole-tree check over the merged content --------------------

post_merge_check = load_script(".github/resolver/auto-resolve/_post_merge_check.py")


def _stub_typecheck(tmp_path, monkeypatch, body: str) -> Path:
    """The caller's check, on PATH and recording the argv it was run with, plus
    the input that names it."""
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    log = tmp_path / "typecheck.log"
    stub = binaries / "typecheck"
    stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"{log}"\n{body}\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")
    monkeypatch.setenv("AUTO_RESOLVE_POST_MERGE_CHECK", "typecheck --project .")
    return log


def test_a_failing_post_merge_check_reports_and_still_bundles(
    step, tmp_path, monkeypatch, capsys
):
    """The resolution lands on the pull request's OWN head, so its checks read this
    same tree and report the same finding — on a branch whose conflict is gone.
    Refusing would spend the whole resolve and hand a human both problems."""
    log = _stub_typecheck(tmp_path, monkeypatch, "exit 3")
    _stub_gh(tmp_path, monkeypatch)
    finding = post_merge_check.run(untrusted_head=False)
    assert log.read_text(encoding="utf-8") == "--project .\n"
    assert "exited 3" in capsys.readouterr().out
    assert "typecheck --project ." in finding
    # Published by `land`, from the bundle — never here. `land` rewrites the sticky
    # comment unconditionally on this very path, so a comment written now is gone
    # by the time the author reads the pull request.
    assert not (tmp_path / "gh.log").exists()


def test_a_reported_finding_quotes_the_check_s_own_report(step, tmp_path, monkeypatch):
    """The comment is the only place this report survives: the next run overwrites the
    comment, the run log ages out, and nothing on the Actions list says which dispatch
    read which pull request. The fence grows past the longest run of backticks the
    report holds, so a report that quotes a fenced block of its own does not end the
    quote early and spill the rest as prose."""
    _stub_typecheck(
        tmp_path,
        monkeypatch,
        "printf \"%s\\n\" 'a.py:1: two definitions of `x`' '```' 'x = 1' '```'\nexit 3",
    )
    _stub_gh(tmp_path, monkeypatch)
    finding = post_merge_check.run(untrusted_head=False)
    assert "a.py:1: two definitions of `x`" in finding
    assert "````" in finding


def test_a_reported_finding_folds_the_whole_report_under_its_preview(
    step, tmp_path, monkeypatch
):
    """One check command runs several tools, and the tool that FAILED is often not the
    last to print — so a preview alone quotes a passing tool and hides the finding the
    comment exists to deliver. The preview says it dropped something, and the fold
    under it holds every line the check printed."""
    _stub_typecheck(
        tmp_path,
        monkeypatch,
        "printf 'a.py:1: two definitions of `x`\\n'\n"
        'seq 1 50 | sed "s/^/pyright checked line /"\nexit 3',
    )
    _stub_gh(tmp_path, monkeypatch)
    finding = post_merge_check.run(untrusted_head=False)
    preview, details, folded = finding.partition("<details>")
    assert details
    assert "…earlier output dropped" in preview
    assert "pyright checked line 50" in preview
    assert "two definitions of `x`" not in preview
    assert "two definitions of `x`" in folded
    assert "pyright checked line 50" in folded


def test_a_folded_report_too_big_for_a_comment_drops_only_its_middle(
    step, tmp_path, monkeypatch
):
    """A comment holds 65536 characters, so a report past the cap still loses lines.
    It loses them from the middle: the banner that names what ran and the last words
    are the two ends a reader needs to place the rest."""
    _stub_typecheck(
        tmp_path,
        monkeypatch,
        "printf 'run banner\\n'\nseq 1 20000 | sed \"s/^/finding /\"\nexit 3",
    )
    _stub_gh(tmp_path, monkeypatch)
    finding = post_merge_check.run(untrusted_head=False)
    assert "run banner" in finding
    assert "finding 20000" in finding
    assert "characters dropped from the middle" in finding
    # 65536 is what a pull request comment holds, and land.sh composes this note with
    # the run's other ones — so a cap widened past what a comment takes reds HERE. A
    # bound derived from the cap cannot: it widens with the thing it guards.
    assert len(finding) < 65_536


def test_a_reported_finding_bounds_the_fences_it_renders_too(
    step, tmp_path, monkeypatch
):
    """The fence grows with the longest backtick run the report holds, so a report made
    of backticks makes the DELIMITERS as long as the report. GitHub rejects a comment
    past 65536 characters whole, so an unbounded pair publishes no diagnosis at all."""
    _stub_typecheck(tmp_path, monkeypatch, "printf '%.0s`' $(seq 1 40000)\nexit 3")
    _stub_gh(tmp_path, monkeypatch)
    finding = post_merge_check.run(untrusted_head=False)
    assert len(finding) < 65_536
    assert "typecheck --project ." in finding


def test_a_reported_finding_redacts_a_credential_the_check_printed(
    step, tmp_path, monkeypatch
):
    """The check is defined by the pull request's own head and runs with every model
    credential in the environment. The job log masks a registered secret and a public
    comment masks nothing, so the value never reaches the comment. The NUL byte in the
    same report is dropped for a different reason: the body crosses into a child
    process's environment, where a NUL raises and would lose the whole refusal."""
    monkeypatch.setenv("FAR_ANTHROPIC_API_KEY", "sk-ant-notarealkey-0123456789")
    _stub_typecheck(
        tmp_path,
        monkeypatch,
        "printf 'a.py:1: %s\\0 leaked\\n' \"$FAR_ANTHROPIC_API_KEY\"\nexit 3",
    )
    _stub_gh(tmp_path, monkeypatch)
    finding = post_merge_check.run(untrusted_head=False)
    assert "sk-ant-notarealkey-0123456789" not in finding
    assert "a.py:1: [redacted] leaked" in finding


def test_a_failing_post_merge_check_gets_one_repair_pass_before_the_handoff(
    step, tmp_path, monkeypatch
):
    """The check is the one reader that sees the merge as a program, so its red is
    usually a file git text-merged into something that does not run. That is the
    repair pass's own defect class, so the tree gets one pass and a second run of
    the check judges what it wrote."""
    log = _stub_typecheck(
        tmp_path,
        monkeypatch,
        f'[[ -e "{tmp_path}/repaired" ]] || {{ echo "NameError: _in" >&2; exit 3; }}',
    )
    reports = []

    def repair(report: Path) -> bool:
        reports.append(report.read_text(encoding="utf-8"))
        (tmp_path / "repaired").touch()
        return True

    post_merge_check.run(untrusted_head=False, repair=repair)
    assert log.read_text(encoding="utf-8") == "--project .\n--project .\n"
    assert reports == ["NameError: _in\n"]


def test_a_post_merge_repair_goes_back_through_the_content_gates(
    step, tmp_path, monkeypatch
):
    """The post-merge check is the LAST gate, so a repair answering it alone would
    reach the bundle judged by none of the ones before it — a formatting violation,
    or a generated file no build produces."""
    ran = []
    monkeypatch.setattr(type(step), "repair_merged_tree", lambda *_a: True)
    for gate in (
        "verify_resolved_content",
        "verify_merge_carried_content",
        "verify_generated_artifacts",
    ):
        monkeypatch.setattr(type(step), gate, lambda _self, name=gate: ran.append(name))

    assert step.repair_and_reverify(tmp_path / "report.txt", "the check") is True

    assert ran == [
        "verify_resolved_content",
        "verify_merge_carried_content",
        "verify_generated_artifacts",
    ]


def test_a_repair_that_never_RAN_re_verifies_nothing(step, tmp_path, monkeypatch):
    """A pass that could not run wrote nothing, so re-running the gates would spend
    three checks to re-judge bytes nobody touched."""
    ran = []
    monkeypatch.setattr(type(step), "repair_merged_tree", lambda *_a: False)
    monkeypatch.setattr(
        type(step), "verify_resolved_content", lambda _self: ran.append("ran")
    )

    assert step.repair_and_reverify(tmp_path / "report.txt", "the check") is False

    assert ran == []


def test_a_check_that_writes_only_on_the_RE_RUN_is_still_refused(
    step, tmp_path, monkeypatch, capsys
):
    """The re-run meets the same read-only gate as the first attempt. Every
    confinement and lint check ran before this, so a file the check stages on its
    second invocation would reach the bundle judged by none of them."""
    _stub_typecheck(
        tmp_path,
        monkeypatch,
        f'[[ -e "{tmp_path}/repaired" ]] || exit 3\n'
        f'printf x >"{Path.cwd()}/{CONFLICTED}"\ngit add -- {CONFLICTED}\nexit 0',
    )
    _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        post_merge_check.run(
            untrusted_head=False,
            repair=lambda _r: bool((tmp_path / "repaired").touch()) or True,
        )
    assert "MODIFIED the tree" in capsys.readouterr().out


def test_a_RE_RUN_that_never_ran_is_named_as_plumbing_too(
    step, tmp_path, monkeypatch, capsys
):
    """127 on the second attempt means the same thing it means on the first: the
    command never reported, so the merge is unjudged rather than bad."""
    _stub_typecheck(
        tmp_path,
        monkeypatch,
        f'[[ -e "{tmp_path}/repaired" ]] || exit 3\nexit 127',
    )
    _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        post_merge_check.run(
            untrusted_head=False,
            repair=lambda _r: bool((tmp_path / "repaired").touch()) or True,
        )
    out = capsys.readouterr().out
    assert "could not RUN" in out
    assert "handed off" not in out


def test_a_repair_that_leaves_the_check_red_still_reports_the_finding(
    step, tmp_path, monkeypatch, capsys
):
    """A pass that ran is not a pass that fixed it, so the second run is what
    decides. Trusting the repair would bundle this merge saying nothing."""
    _stub_typecheck(tmp_path, monkeypatch, "exit 3")
    _stub_gh(tmp_path, monkeypatch)
    post_merge_check.run(untrusted_head=False, repair=lambda _report: True)
    assert "exited 3" in capsys.readouterr().out


def test_a_passing_post_merge_check_lets_the_resolution_through(
    step, tmp_path, monkeypatch
):
    log = _stub_typecheck(tmp_path, monkeypatch, "exit 0")
    post_merge_check.run(untrusted_head=False)
    assert log.read_text(encoding="utf-8") == "--project .\n"


def test_a_check_that_never_RAN_is_named_as_plumbing_not_as_a_bad_merge(
    step, tmp_path, monkeypatch, capsys
):
    """126, 127 and 128+signal all mean the command never reported, so the merge is
    unjudged rather than bad. `.github/scripts/pyright-passes.sh` re-raises exactly
    those, and telling their author to fix a type error that does not exist is what
    makes a repo-wide outage undiagnosable."""
    _stub_typecheck(tmp_path, monkeypatch, "exit 127")
    _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        post_merge_check.run(untrusted_head=False)
    out = capsys.readouterr().out
    assert "could not RUN" in out
    # A plumbing refusal takes NO attempt mark: the fix lands outside the pull
    # request, so a re-run against this same head answers differently.
    assert "handed off" not in out
    comment = status_comments((tmp_path / "gh.log").read_text(encoding="utf-8"))[0]
    assert "provisioning" in comment


def test_a_check_that_DIED_importing_its_own_dependency_is_plumbing_too(
    step, tmp_path, monkeypatch, capsys
):
    """The case the exit-code floor missed: an unpinned dependency of the caller's
    check exits 1, which is what a check reporting a type error exits. Publishing
    that as a finding blames the branch for a tree nothing read."""
    _stub_typecheck(
        tmp_path,
        monkeypatch,
        "echo \"ModuleNotFoundError: No module named 'yaml'\" >&2\nexit 1",
    )
    _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        post_merge_check.run(untrusted_head=False)
    out = capsys.readouterr().out
    assert "could not RUN" in out
    assert "handed off" not in out


def test_a_check_that_died_on_a_module_THE_MERGED_TREE_holds_is_a_finding(
    step, tmp_path, monkeypatch
):
    """A merge can break a LOCAL import — one side renames the module, the other
    still imports it. That failure is the merged tree's own, so it is reported as
    a finding rather than as this workflow's provisioning."""
    (tmp_path / "work" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path / "work", "add", "helper.py")
    _stub_typecheck(
        tmp_path,
        monkeypatch,
        "echo \"ModuleNotFoundError: No module named 'helper'\" >&2\nexit 1",
    )
    _stub_gh(tmp_path, monkeypatch)
    finding = post_merge_check.run(untrusted_head=False)
    assert "still needs your attention" in finding
    assert "No module named 'helper'" in finding


def test_a_check_that_WRITES_is_refused_rather_than_bundled(
    step, tmp_path, monkeypatch, capsys
):
    """Every confinement, generated-artifact and lint check ran before this one, so
    a file the check staged would reach the bundle judged by none of them."""
    _stub_typecheck(
        tmp_path, monkeypatch, 'printf "formatted\\n" >a.md\ngit add -- a.md\nexit 0'
    )
    _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        post_merge_check.run(untrusted_head=False)
    assert "MODIFIED the tree" in capsys.readouterr().out


def test_a_post_merge_check_that_outruns_its_budget_becomes_a_finding(
    step, tmp_path, monkeypatch, capsys
):
    """The check gets a wall-clock bound, and everything it started dies with it.

    Unbounded, the four invocations spend the resolve job's entire
    `timeout-minutes` and GitHub kills the run with the conflict resolved and
    nothing pushed. `subprocess.run(timeout=...)` alone only half-answers that:
    it kills the direct child and waits for that child alone, so the stub's
    background subshell here outlives the bound and keeps writing into the tree the
    bundle is about to read."""
    monkeypatch.setenv("POST_MERGE_CHECK_BUDGET_SECONDS", "0.3")
    alive = tmp_path / "alive"
    _stub_typecheck(
        tmp_path,
        monkeypatch,
        f'echo "reached the type checker"\n'
        f'( while :; do : >"{alive}"; sleep 0.05; done ) &\nsleep 30',
    )
    _stub_gh(tmp_path, monkeypatch)
    finding = post_merge_check.run(untrusted_head=False)
    assert "ceiling is 0.3s" in finding
    assert "POST_MERGE_CHECK_BUDGET_SECONDS" in finding
    printed = capsys.readouterr().out
    assert "did not finish" in printed
    # The killed check's own words are the only thing that says WHERE it hung, and
    # the overrun path is otherwise the one failure that quotes nothing.
    assert "reached the type checker" in printed
    alive.unlink(missing_ok=True)
    # allow-sleep: the subject IS the bound. A survivor announces itself every 50ms,
    # so only waiting out ten of its rounds can show that none did.
    time.sleep(0.5)
    assert not alive.exists()


def test_a_group_kill_the_kernel_REFUSES_still_bounds_the_check(
    step, tmp_path, monkeypatch
):
    """The group signal is suppressed on two errors, and neither one killed anything.

    `Popen.__exit__` then waits on the direct child with no bound, so the run spends
    the resolve job's whole cap inside a check it had already given up on, and
    pushes nothing at all. The signal is what the kernel refuses here; the direct
    child is the one process this runner definitely owns. What remains after that
    kill is the DRAIN, whose own bound covers the survivors still holding the pipes
    — shortened here so the case costs a second rather than the stub's whole sleep."""
    monkeypatch.setenv("POST_MERGE_CHECK_BUDGET_SECONDS", "0.3")
    monkeypatch.setattr(refusal, "_DRAIN_SECONDS", 1.0)

    def refuses_the_group(*_args):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", refuses_the_group)
    _stub_typecheck(tmp_path, monkeypatch, "sleep 25")
    _stub_gh(tmp_path, monkeypatch)
    started = time.monotonic()
    finding = post_merge_check.run(untrusted_head=False)
    assert "ceiling is 0.3s" in finding
    elapsed = time.monotonic() - started
    # allow-wall-clock: the wall clock IS the bound under test, and the stub's own
    # 25 seconds is three times the widest reading a loaded runner produces here.
    assert elapsed < 15, f"waited {elapsed:.1f}s — the command outlived the bound"


def test_a_post_merge_check_that_WROTE_before_it_overran_is_still_refused(
    step, tmp_path, monkeypatch, capsys
):
    """The wall-clock bound is not a way past the read-only gate.

    `commit_the_merge` runs straight after this, so a formatter or generator killed
    at the bound has already staged what it wrote, and reporting the overrun without
    reading the tree pushes those bytes past every confinement and lint check that
    ran before them."""
    monkeypatch.setenv("POST_MERGE_CHECK_BUDGET_SECONDS", "0.3")
    _stub_typecheck(
        tmp_path,
        monkeypatch,
        f'printf x >"{Path.cwd()}/{CONFLICTED}"\ngit add -- {CONFLICTED}\nsleep 30',
    )
    _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        post_merge_check.run(untrusted_head=False)
    assert "MODIFIED the tree" in capsys.readouterr().out


def test_a_post_merge_run_spends_the_deadline_it_was_HANDED(
    step, tmp_path, monkeypatch, capsys
):
    """One resolve, one budget — the self-review gate's run does not get a new one.

    A resolve calls `run` twice at top level: once from the bundle step, then again
    over what the self-review fixer wrote. A deadline stamped inside each call gives
    the second the whole budget again, so the pair can spend twice what
    `auto-resolve.yaml` charges and the job dies with the merge resolved and nothing
    pushed. Handed a deadline the first call already spent, the second must report
    the overrun rather than start the 600 seconds over — and must not START the
    command either, because the millisecond `_left` floors at is long enough for a
    formatter to stage a file, which refuses the resolution this finding saves."""
    monkeypatch.setenv("POST_MERGE_CHECK_BUDGET_SECONDS", "600")
    _stub_typecheck(tmp_path, monkeypatch, "sleep 30")
    _stub_gh(tmp_path, monkeypatch)
    # At the launch seam rather than on the stub's own log: killed a millisecond in,
    # the stub loses the race to its first line, so a check that DID start leaves the
    # same empty tree behind and the log would report the fix working either way.
    monkeypatch.setattr(
        post_merge_check,
        "_read_the_tree",
        lambda *_: pytest.fail("launched a check the budget could only kill"),
    )
    finding = post_merge_check.run(
        untrusted_head=False, deadline=time.monotonic() - 1.0
    )
    assert "ceiling is 600s" in finding
    assert "did not finish" in capsys.readouterr().out


def test_the_post_merge_budget_never_outlives_the_resolve_JOB(monkeypatch):
    """A check that outlives `timeout-minutes` costs the caller the resolution.

    GitHub kills the job at that cap with the merge resolved and NOTHING pushed, so
    the configured budget is a ceiling and the job's own remaining wall clock is the
    other one. The reserve is what the job keeps for staging its logs."""
    monkeypatch.setenv("POST_MERGE_CHECK_BUDGET_SECONDS", "600")
    monkeypatch.setenv("POST_MERGE_CHECK_RESERVE_MINUTES", "5")
    monkeypatch.setenv("AUTO_RESOLVE_JOB_DEADLINE_EPOCH", str(int(time.time()) + 400))
    # 400s to the job's cap, less the 5-minute reserve, leaves 100.
    clamped = post_merge_check.new_budget() - time.monotonic()
    assert 90 < clamped < 105, clamped  # allow-wall-clock: the budget IS a clock
    # The other direction, so the clamp cannot harden into "always the job's left".
    monkeypatch.setenv("AUTO_RESOLVE_JOB_DEADLINE_EPOCH", str(int(time.time()) + 9000))
    whole = post_merge_check.new_budget() - time.monotonic()
    assert 595 < whole <= 600, whole  # allow-wall-clock: as above


def test_a_resolve_that_stamped_no_job_deadline_keeps_its_whole_ceiling(monkeypatch):
    """The stamp is a step in `auto-resolve.yaml`, so anything driving these modules
    outside that job has none — and an absent or malformed one must not read as a
    budget of zero, which would leave every merge unchecked."""
    monkeypatch.setenv("POST_MERGE_CHECK_BUDGET_SECONDS", "600")
    # allow-wall-clock: the budget IS a wall clock, so a duration is its only
    # observable. Every reading here must be the whole unclamped ceiling.
    for raw in ("", "   ", "not-an-epoch", "-5"):
        monkeypatch.setenv("AUTO_RESOLVE_JOB_DEADLINE_EPOCH", raw)
        whole = post_merge_check.new_budget() - time.monotonic()
        assert 595 < whole <= 600, raw  # allow-wall-clock: as above
    monkeypatch.delenv("AUTO_RESOLVE_JOB_DEADLINE_EPOCH")
    unstamped = post_merge_check.new_budget() - time.monotonic()
    assert 595 < unstamped <= 600  # allow-wall-clock: as above


def test_one_resolve_stamps_ONE_post_merge_deadline(step):
    """The memo behind the shared deadline above. The bundle step and the self-review
    gate each ask for it, and a second stamp hands the pair twice what the job
    charges — the exact overspend the handed deadline exists to stop."""
    assert step.post_merge_deadline() == step.post_merge_deadline()


def test_a_check_that_only_READS_leaves_the_tree_alone(step, tmp_path, monkeypatch):
    """The other direction, so the guard cannot harden into "any check is a writer"."""
    _stub_typecheck(tmp_path, monkeypatch, "git status --porcelain >/dev/null\nexit 0")
    post_merge_check.run(untrusted_head=False)


def test_a_fork_head_runs_no_post_merge_check_binary_at_all(tmp_path, monkeypatch):
    """The untrusted-head boundary. The command is a script the fork's own manifest
    defines, and the resolve job holds every model credential. The stub stays on
    PATH and exits 3, so a run would have refused loudly."""
    log = _stub_typecheck(tmp_path, monkeypatch, "exit 3")
    post_merge_check.run(untrusted_head=True)
    assert not log.exists()


def test_a_caller_that_named_no_post_merge_check_runs_none(tmp_path, monkeypatch):
    """A caller with no whole-tree check is not a caller this resolver refuses."""
    log = _stub_typecheck(tmp_path, monkeypatch, "exit 3")
    monkeypatch.delenv("AUTO_RESOLVE_POST_MERGE_CHECK")
    post_merge_check.run(untrusted_head=False)
    assert not log.exists()


def test_a_post_merge_check_whose_SCRIPT_the_tree_lacks_configures_no_check(
    step, tmp_path, monkeypatch, capsys
):
    """A branch whose head and base both fork from before the check script landed
    carries no such file, so `bash <it>` exits 127 and reads as a missing tool. It is
    neither: that branch configured no check, and it cannot add one — the file it
    lacks is on the default branch, which is not its base. Every conflict on such a
    branch cost a hand resolution."""
    _stub_gh(tmp_path, monkeypatch)
    monkeypatch.setenv(
        "AUTO_RESOLVE_POST_MERGE_CHECK", "bash .github/scripts/not-here.sh"
    )
    post_merge_check.run(untrusted_head=False)
    assert "does not contain" in capsys.readouterr().out
    assert not (tmp_path / "gh.log").exists()


def test_a_check_the_BASE_already_fails_names_the_base_and_not_the_conflict(
    tmp_path, monkeypatch
):
    """The check reads the merge as a program, so a defect either parent already
    carries reds the merged tree too. Blaming the conflict there sends the reader to
    the wrong file: the merge has nothing to resolve differently.

    The base side alone carries `b.md`, so naming the head too would be wrong — which
    is what tells this apart from an implementation that runs neither parent and
    names both."""
    _bundle_step(
        tmp_path,
        monkeypatch,
        _repo(tmp_path, main_extra={"b.md": "main b\n"}),
        CONFLICTED,
    )
    base_sha = post_merge_check.git("rev-parse", "MERGE_HEAD").strip()
    head_sha = post_merge_check.git("rev-parse", "HEAD").strip()
    _stub_typecheck(tmp_path, monkeypatch, "test -f b.md && exit 3\nexit 0")
    _stub_gh(tmp_path, monkeypatch)
    finding = post_merge_check.run(
        untrusted_head=False, head_sha=head_sha, base_sha=base_sha
    )
    assert "the base branch" in finding
    assert "this pull request's head" not in finding
    assert "Leaving the conflict for a human to resolve" not in finding
    # Published by `land`, from the bundle — never here.
    assert not (tmp_path / "gh.log").exists()


def test_attribution_is_SKIPPED_when_too_little_budget_is_left_to_run_it(
    tmp_path, monkeypatch
):
    """Same repository and same check as the case above, and only the budget differs.

    Attribution costs two more checkouts and two more runs of the caller's command,
    and it only says who owns a failure the finding already names. Under the floor
    that buys runs the bound can only kill, so the plain finding is the right one."""
    _bundle_step(
        tmp_path,
        monkeypatch,
        _repo(tmp_path, main_extra={"b.md": "main b\n"}),
        CONFLICTED,
    )
    base_sha = post_merge_check.git("rev-parse", "MERGE_HEAD").strip()
    head_sha = post_merge_check.git("rev-parse", "HEAD").strip()
    _stub_typecheck(tmp_path, monkeypatch, "test -f b.md && exit 3\nexit 0")
    _stub_gh(tmp_path, monkeypatch)
    finding = post_merge_check.run(
        untrusted_head=False,
        head_sha=head_sha,
        base_sha=base_sha,
        deadline=time.monotonic() + 5.0,
    )
    assert "the base branch" not in finding
    assert "the one repair pass could not correct what it found" in finding


def test_a_path_shaped_ARGUMENT_the_tree_lacks_does_not_skip_the_check(
    step, tmp_path, monkeypatch, capsys
):
    """An ordinary check command carries path-shaped arguments that are not files in
    the worktree. Reading one as an absent script reports `no check configured` and
    bundles a merge the one reader that sees it as a program never judged."""
    log = _stub_typecheck(tmp_path, monkeypatch, "exit 3")
    monkeypatch.setenv(
        "AUTO_RESOLVE_POST_MERGE_CHECK", "typecheck --changed-since origin/main"
    )
    _stub_gh(tmp_path, monkeypatch)
    post_merge_check.run(untrusted_head=False)
    assert log.read_text(encoding="utf-8") == "--changed-since origin/main\n"
    assert "exited 3" in capsys.readouterr().out


def test_a_post_merge_check_binary_the_runner_lacks_is_named_as_plumbing(
    step, tmp_path, monkeypatch, capsys
):
    """No shell stands between the run and the command, so a name that is not on
    PATH RAISES rather than reporting 127 — past `_refuse_a_check_that_never_ran`,
    the arm written for exactly this. The raise lost a resolution the model had
    already been billed for, and reported it as a merge the resolver could not do.

    It takes no handoff mark, so a re-run after the caller installs the tool
    checks this same resolution instead of waiting out the mark's TTL."""
    _stub_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTO_RESOLVE_POST_MERGE_CHECK", "not-an-installed-tool .")
    with pytest.raises(SystemExit):
        post_merge_check.run(untrusted_head=False)
    assert "will not run on this runner" in capsys.readouterr().out
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "installs no such binary" in log
    assert "auto-resolve/handed-off" not in log


def test_a_post_merge_check_script_without_its_exec_bit_is_named_as_plumbing(
    step, tmp_path, monkeypatch, capsys
):
    """The OTHER `OSError` this refusal covers, and why its advice cannot say
    "install it": git tracks the exec bit, so a check script committed 100644 is
    present and unrunnable, and installing nothing fixes that. Even a root runner
    gets EACCES here, because no x bit is set for any class."""
    script = tmp_path / "check.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    script.chmod(0o644)
    _stub_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTO_RESOLVE_POST_MERGE_CHECK", str(script))
    with pytest.raises(SystemExit):
        post_merge_check.run(untrusted_head=False)
    assert "will not run on this runner" in capsys.readouterr().out
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "not executable" in log
    assert "auto-resolve/handed-off" not in log


# --- the lint gate over the resolved content ---------------------------------


def _stub_precommit(tmp_path, monkeypatch, body: str) -> Path:
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    log = tmp_path / "precommit.log"
    stub = binaries / "pre-commit"
    stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"{log}"\n{body}\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")
    return log


def test_an_empty_staged_set_lints_nothing(step, tmp_path, monkeypatch):
    """Empty on prepare's clean-merge path, which authored no content to lint."""
    log = _stub_precommit(tmp_path, monkeypatch, "exit 0")
    step.verify_resolved_content()
    assert not log.exists()


def test_a_missing_pre_commit_is_named_as_provisioning(step, tmp_path, monkeypatch):
    step.staged = [CONFLICTED]
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    _stub_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", f"{binaries}:/usr/bin:/bin")
    with pytest.raises(SystemExit):
        step.verify_resolved_content()


def test_clean_hooks_pass_in_one_run(step, tmp_path, monkeypatch):
    log = _stub_precommit(tmp_path, monkeypatch, "exit 0")
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    step.verify_resolved_content()
    assert log.read_text(encoding="utf-8").strip() == f"run --files {CONFLICTED}"


def test_an_auto_fix_is_re_staged_and_verified_a_second_time(
    step, tmp_path, monkeypatch
):
    """The same fix-then-verify contract a normal hook-run commit gets."""
    marker = tmp_path / "ran"
    log = _stub_precommit(
        tmp_path,
        monkeypatch,
        f'if [[ -f "{marker}" ]]; then exit 0; fi\ntouch "{marker}"\nexit 1',
    )
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    step.verify_resolved_content()
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_a_hook_rewritten_lockfile_is_staged_not_refused(step, tmp_path, monkeypatch):
    """The repo's own regen hook IS the lock command, so its lockfile rewrite is
    staged and re-verified instead of reading as a stray write no repair grant
    may touch (agent-glovebox#5207 arm 2). The model-may-not-write-a-lockfile
    grant is untouched: nothing hands the path to a repair pass."""
    (Path.cwd() / "uv.lock").write_text("locked\n", encoding="utf-8")
    git_io.git("add", "--", "uv.lock")
    marker = tmp_path / "ran"
    _stub_precommit(
        tmp_path,
        monkeypatch,
        f'if [[ -f "{marker}" ]]; then exit 0; fi\ntouch "{marker}"\n'
        f'echo relocked >"{Path.cwd() / "uv.lock"}"\nexit 1',
    )
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    step.verify_resolved_content()
    assert git_io.git("show", ":uv.lock") == "relocked\n"
    assert git_io.git("diff", "--name-only").strip() == ""


def test_a_lockfile_a_PASSING_hook_rewrote_is_staged_too(step, tmp_path, monkeypatch):
    """The failure arm is not the only one: a regen hook that re-derives `uv.lock`
    and exits 0 never reaches a recheck, so only the stage before the stray-file
    scan keeps the run from discarding the resolution."""
    (Path.cwd() / "uv.lock").write_text("locked\n", encoding="utf-8")
    git_io.git("add", "--", "uv.lock")
    _stub_precommit(
        tmp_path, monkeypatch, f'echo relocked >"{Path.cwd() / "uv.lock"}"\nexit 0'
    )
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    step.verify_resolved_content()
    assert git_io.git("show", ":uv.lock") == "relocked\n"
    assert git_io.git("diff", "--name-only").strip() == ""


def test_a_second_failure_refuses_the_bundle(step, tmp_path, monkeypatch):
    _stub_precommit(tmp_path, monkeypatch, 'echo "ruff.....Failed"; exit 1')
    _stub_gh(tmp_path, monkeypatch)
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    with pytest.raises(SystemExit):
        step.verify_resolved_content()


def test_a_hook_that_could_not_start_blames_the_job_not_the_resolution(
    step, tmp_path, monkeypatch, capsys
):
    """Reaching the caller's rejection message here would tell a human the
    resolution failed a check that never ran."""
    _stub_precommit(
        tmp_path, monkeypatch, 'echo "Executable \\`shellcheck\\` not found"; exit 1'
    )
    _stub_gh(tmp_path, monkeypatch)
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    with pytest.raises(SystemExit):
        step.verify_resolved_content()
    assert "could not RUN in this job" in capsys.readouterr().out


def test_a_hook_that_rewrote_a_file_outside_the_set_is_refused(
    step, tmp_path, monkeypatch, capsys
):
    """A hook that rewrote something OUTSIDE the resolved set leaves it unstaged
    and thus out of this commit, producing a tree whose hooks disagree with its
    content."""
    _stub_precommit(tmp_path, monkeypatch, 'printf "rewritten\\n" > other.md\nexit 0')
    _stub_gh(tmp_path, monkeypatch)
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    with pytest.raises(SystemExit):
        step.verify_resolved_content()
    assert "outside the resolved set" in capsys.readouterr().out


# --- the lint gate over what the merge itself carried ------------------------


def _carry_a_clean_change(work: Path, name: str = "other.md") -> None:
    """Redo the fixture's merge with a second file BOTH sides change, at
    non-overlapping lines: git text-merges it by itself, so it reaches the
    index without anybody resolving it, and without either side's edit
    matching the merge result on its own (`merge_carried_paths` requires a
    change relative to EACH parent, not just one)."""
    _git(work, "merge", "--abort")
    _git(work, "checkout", "-q", "feature")
    (work / name).write_text("feature line\nbase\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "a clean change on feature")
    _git(work, "checkout", "-q", "main")
    (work / name).write_text("base\nmain line\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "a clean change on main")
    _git(work, "checkout", "-q", "feature")
    subprocess.run(
        ["git", "-C", str(work), "merge", "--no-edit", "main"],
        capture_output=True,
        check=False,
    )


def test_a_merge_with_nothing_but_the_resolution_lints_nothing_extra(
    step, tmp_path, monkeypatch
):
    log = _stub_precommit(tmp_path, monkeypatch, "exit 0")
    step.read_parents()
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    step.verify_merge_carried_content()
    assert not log.exists()


def test_the_paths_the_merge_carried_are_linted_apart_from_the_resolved_set(
    step, tmp_path, monkeypatch
):
    """The bytes git produced on its own are the ones nothing else checks."""
    log = _stub_precommit(tmp_path, monkeypatch, "exit 0")
    _carry_a_clean_change(Path.cwd())
    step.read_parents()
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    step.verify_merge_carried_content()
    assert log.read_text(encoding="utf-8").strip() == "run --files other.md"


def test_merge_carried_content_that_fails_the_hooks_lands_and_is_flagged(
    step, tmp_path, monkeypatch, capsys
):
    """The conflicts are resolved by the time this runs, so a hook failure in
    files nobody resolved flags the merge instead of discarding it. The pull
    request's own pre-commit check judges these bytes after the push."""
    _stub_precommit(tmp_path, monkeypatch, 'echo "ruff.....Failed"; exit 1')
    _carry_a_clean_change(Path.cwd())
    monkeypatch.setenv("BASE_REF", "main")
    _stub_gh(tmp_path, monkeypatch)
    step.read_parents()
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    step.verify_merge_carried_content()
    assert step.carried_hook_failures == ["other.md"]
    assert "landing the resolution and flagging it" in capsys.readouterr().out


def test_a_carried_hook_failure_reaches_land_through_the_bundle(step):
    """`land` learns of it the way it learns of a declined path: a file beside
    the bundle. Without it the note never reaches the PR and auto-merge stays
    armed over a resolution whose pre-commit check will be red."""
    _committed_merge(step)
    step.read_parents()
    step.carried_hook_failures = ["other.md"]
    step.write_the_bundle()
    sentinel = step.bundle_dir / "carried-hook-failed"
    assert sentinel.read_text(encoding="utf-8") == "other.md\n"


def test_a_clean_merge_leaves_no_carried_hook_marker(step):
    _committed_merge(step)
    step.read_parents()
    step.write_the_bundle()
    assert not (step.bundle_dir / "carried-hook-failed").exists()


def test_an_out_of_conflict_rewrite_reaches_land_through_the_bundle(step):
    """The lines landed because the revert was ambiguous, so this file is the only
    thing that tells `land` to name them and disarm auto-merge. Without it the
    change sits in a merge commit that neither the PR diff nor any note shows."""
    _committed_merge(step)
    step.read_parents()
    step.out_of_conflict_rewrites = ["other.md\t12-18"]
    step.write_the_bundle()
    sidecar = step.bundle_dir / "rewrote-outside-conflict"
    assert sidecar.read_text(encoding="utf-8") == "other.md\t12-18\n"


def test_a_clean_merge_leaves_no_out_of_conflict_marker(step):
    _committed_merge(step)
    step.read_parents()
    step.write_the_bundle()
    assert not (step.bundle_dir / "rewrote-outside-conflict").exists()


def test_a_hook_that_rewrote_a_merge_carried_file_is_refused(
    step, tmp_path, monkeypatch, capsys
):
    """An auto-format nothing rejected is not a defect worth widening the merge
    for: a hook that rewrote these bytes without failing would commit a change
    nobody reviewed under the merge's name, so no repair pass runs."""
    _stub_precommit(tmp_path, monkeypatch, 'printf "rewritten\\n" > other.md\nexit 0')
    _carry_a_clean_change(Path.cwd())
    _stub_gh(tmp_path, monkeypatch)
    step.read_parents()
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    with pytest.raises(SystemExit):
        step.verify_merge_carried_content()
    assert "modified merge-carried file(s)" in capsys.readouterr().out


# --- the hook-repair pass -----------------------------------------------------


def _stub_repair(tmp_path, monkeypatch, body: str) -> Path:
    """A fake repair.py where repair_hook_failures runs the real one, recording
    which credential each rung ran with. The stub stands in for the paid model
    run only; the ladder walk, the marker guard and the hook re-runs are real."""
    home = tmp_path / "repair-scripts"
    home.mkdir(exist_ok=True)
    log = tmp_path / "repair.log"
    (home / "repair.py").write_text(
        "import os, sys\n"
        # _claude_cli_env_for routes a rung to CLAUDE_CODE_OAUTH_TOKEN or
        # ANTHROPIC_API_KEY by the credential's shape, always clearing the other.
        "token = os.environ['CLAUDE_CODE_OAUTH_TOKEN'] or os.environ['ANTHROPIC_API_KEY']\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(token + '\\n')\n"
        f"{body}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(repair_pass, "_SCRIPT_DIR", home)
    return log


def _claude_on_path(tmp_path, monkeypatch) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir(exist_ok=True)
    stub = binaries / "claude"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")


def test_the_repair_pass_is_skipped_without_a_credential(
    step, tmp_path, monkeypatch, capsys
):
    log = _stub_repair(tmp_path, monkeypatch, "sys.exit(0)")
    assert step.repair_hook_failures(tmp_path / "report.txt") is False
    assert "no hook-repair pass" in capsys.readouterr().out
    assert not log.exists(), "the repair subprocess ran without a credential"


def _installer(tmp_path, monkeypatch, body: str) -> Path:
    """Stand in for install-claude-cli.sh, which would otherwise reach npm."""
    installer = tmp_path / "install-claude-cli.sh"
    installer.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    installer.chmod(0o755)
    monkeypatch.setattr(repair_pass, "_CLI_INSTALLER", installer)
    return installer


def test_the_repair_pass_installs_the_cli_when_the_job_has_none(
    step, tmp_path, monkeypatch, capsys
):
    """The resolve job installs the CLI in the step that runs the model, so a run
    whose conflicts the deterministic pre-pass answered reaches the repair with no
    binary. The pass provisions one and carries on to its next question."""
    monkeypatch.setenv(_LADDER_VARS[0], "tok-primary")
    binaries = tmp_path / "installed-bin"
    binaries.mkdir()
    monkeypatch.setenv(
        "PATH", f"{binaries}:{path_without_binary('claude', base=SYSTEM_PATH_DIRS)}"
    )
    _installer(
        tmp_path,
        monkeypatch,
        f"printf '#!/usr/bin/env bash\\nexit 0\\n' > {binaries}/claude\n"
        f"chmod +x {binaries}/claude",
    )
    step.staged = []
    assert step.repair_hook_failures(tmp_path / "report.txt") is False
    out = capsys.readouterr().out
    assert "no CLI on PATH" not in out
    assert "no file in the rejected set" in out


def test_the_repair_pass_is_skipped_when_the_cli_cannot_be_installed(
    step, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(_LADDER_VARS[0], "tok-primary")
    # A PATH that genuinely resolves no `claude`: a host with the CLI in a system
    # dir would take the credential-and-CLI branch and never print this warning.
    monkeypatch.setenv("PATH", path_without_binary("claude", base=SYSTEM_PATH_DIRS))
    ran = tmp_path / "installer-ran"
    _installer(tmp_path, monkeypatch, f"touch {ran}\nexit 1")
    assert step.repair_hook_failures(tmp_path / "report.txt") is False
    assert ran.exists(), "the pass skipped without trying to install the CLI"
    assert "no CLI on PATH" in capsys.readouterr().out


def _grant_recording_step(tmp_path, monkeypatch):
    """A step mid-merge where `b.md` is the BASE side's own landed change, and a
    stub repair.py that records the write grant it was handed."""
    step = _bundle_step(
        tmp_path,
        monkeypatch,
        _repo(tmp_path, main_extra={"b.md": "cites a.md\n"}),
        CONFLICTED,
    )
    step.read_parents()
    step.staged = [CONFLICTED]
    _claude_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv(_LADDER_VARS[0], "tok-primary")
    home = tmp_path / "repair-scripts"
    home.mkdir(exist_ok=True)
    grant = tmp_path / "grant.txt"
    (home / "repair.py").write_text(
        "import os\n"
        f"open({str(grant)!r}, 'w', encoding='utf-8').write(os.environ['REPAIR_FILE_LIST'])\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(repair_pass, "_SCRIPT_DIR", home)
    return step, grant


def test_the_repair_grant_covers_the_file_the_failing_hook_NAMED(tmp_path, monkeypatch):
    """A hook rejects the merge over a file no conflict named — a docstring citing
    a path the other side deleted. The repair may edit what refused it, so the
    grant reads the refusal as well as the conflicted set."""
    step, grant = _grant_recording_step(tmp_path, monkeypatch)
    report = tmp_path / "report.txt"
    report.write_text("check-dangling-path-refs\nb.md:1: a.md\n", encoding="utf-8")
    assert step.repair_hook_failures(report) is False
    # not-a-drift-guard: the equality is the observed write grant the pass built,
    # not a second copy of a list some source owns.
    assert sorted(grant.read_text(encoding="utf-8").split()) == ["a.md", "b.md"]


def test_the_repair_grant_refuses_a_path_the_MERGE_never_changed(tmp_path, monkeypatch):
    """The report is untrusted: a hook runs in the merged tree and prints whatever
    that tree's own content makes it print. `untouched.md` is tracked and both
    parents leave it alone, so naming it must buy no write grant."""
    step, grant = _grant_recording_step(tmp_path, monkeypatch)
    report = tmp_path / "report.txt"
    report.write_text("some-hook\nuntouched.md:1: whatever\n", encoding="utf-8")
    assert step.repair_hook_failures(report) is False
    assert grant.read_text(encoding="utf-8").split() == ["a.md"]


def test_the_repair_grant_refuses_a_path_the_report_only_MENTIONS(
    tmp_path, monkeypatch
):
    """Hook text names files as advice as readily as as sites — "declare it in
    `b.md`". A mention mid-sentence is not an objection, and granting the file
    that DEFINES a gate would let the repair satisfy the gate by editing it."""
    step, grant = _grant_recording_step(tmp_path, monkeypatch)
    report = tmp_path / "report.txt"
    report.write_text("some-hook: add an entry to b.md first\n", encoding="utf-8")
    assert step.repair_hook_failures(report) is False
    assert grant.read_text(encoding="utf-8").split() == ["a.md"]


def test_the_hooks_RE_RUN_over_the_file_the_repair_changed(tmp_path, monkeypatch):
    """The grant and the re-verified set are two halves: a repair that edits the
    hook's own file must put that file back through the hooks, or the pass green-
    lights bytes nothing judged."""
    step, _ = _grant_recording_step(tmp_path, monkeypatch)
    home = tmp_path / "repair-scripts"
    (home / "repair.py").write_text(
        "from pathlib import Path\n"
        "Path('b.md').write_text('repaired\\n', encoding='utf-8')\n"
        # The conflicted file too: a marker left anywhere refuses the pass before
        # it re-runs the hooks, which is a different case from this one.
        f"Path({CONFLICTED!r}).write_text('resolved\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    log = _stub_precommit(tmp_path, monkeypatch, "exit 0")
    report = tmp_path / "report.txt"
    report.write_text("check-dangling-path-refs\nb.md:1: a.md\n", encoding="utf-8")
    assert step.repair_hook_failures(report) is True
    assert "b.md" in log.read_text(encoding="utf-8")


def test_the_claude_cli_env_routes_by_credential_shape() -> None:
    """A metered key must arrive as `ANTHROPIC_API_KEY` with `CLAUDE_CODE_OAUTH_TOKEN`
    cleared, and an oauth token the other way round — a regression that routed
    every rung through one variable would leave the metered rung authenticating
    with nothing and the run dying as an unreachable credential."""
    oauth_env = credentials._claude_cli_env_for("sk-ant-oat-live")
    assert oauth_env == {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-live",
        "ANTHROPIC_API_KEY": "",
    }
    metered_env = credentials._claude_cli_env_for("sk-ant-api-live")
    assert metered_env == {
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "ANTHROPIC_API_KEY": "sk-ant-api-live",
    }


def test_the_repair_ladder_routes_a_metered_rung_through_its_own_var(
    step, tmp_path, monkeypatch
):
    """Behavioral counterpart to the unit test above, driven through the real
    ladder walk: an oauth-shaped rung dies, the metered-shaped rung after it
    repairs, and the stub records which env var actually carried the value —
    not merely that `token` (the `or`-joined fixture) came out non-empty."""
    _claude_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv(_LADDER_VARS[0], "sk-ant-oat-dead")
    monkeypatch.setenv(_LADDER_VARS[1], "sk-ant-api-live")
    (Path.cwd() / CONFLICTED).write_text("broken\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    _stub_precommit(tmp_path, monkeypatch, "grep -q broken a.md && exit 1\nexit 0")
    home = tmp_path / "repair-scripts"
    home.mkdir(exist_ok=True)
    log = tmp_path / "env-log.json"
    (home / "repair.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({\n"
        "        'oauth': os.environ['CLAUDE_CODE_OAUTH_TOKEN'],\n"
        "        'api_key': os.environ['ANTHROPIC_API_KEY'],\n"
        "    }) + '\\n')\n"
        "if os.environ['CLAUDE_CODE_OAUTH_TOKEN']:\n"
        "    sys.exit(1)\n"
        "Path('a.md').write_text('repaired\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(repair_pass, "_SCRIPT_DIR", home)
    report = tmp_path / "report.txt"
    assert step.repair_hook_failures(report) is True
    records = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    assert records == [
        {"oauth": "sk-ant-oat-dead", "api_key": ""},
        {"oauth": "", "api_key": "sk-ant-api-live"},
    ]


def test_the_repair_ladder_walks_dead_rungs_and_re_verifies_the_fix(
    step, tmp_path, monkeypatch
):
    """The first credential dies, the second repairs; the hooks then re-judge the
    repaired content through the same fix-then-verify contract."""
    _claude_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv(_LADDER_VARS[0], "tok-dead")
    monkeypatch.setenv(_LADDER_VARS[1], "tok-live")
    (Path.cwd() / CONFLICTED).write_text("broken\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    hook_log = _stub_precommit(
        tmp_path, monkeypatch, "grep -q broken a.md && exit 1\nexit 0"
    )
    log = _stub_repair(
        tmp_path,
        monkeypatch,
        "if token == 'tok-dead':\n"
        "    sys.exit(1)\n"
        "from pathlib import Path\n"
        "Path('a.md').write_text('repaired\\n', encoding='utf-8')\n",
    )
    report = tmp_path / "report.txt"
    assert step.repair_hook_failures(report) is True
    assert log.read_text(encoding="utf-8") == "tok-dead\ntok-live\n"
    # The repaired bytes were re-staged and re-judged, not trusted.
    assert hook_log.read_text(encoding="utf-8").strip() == f"run --files {CONFLICTED}"
    assert "repaired" in git_io.git("diff", "--cached", "--", CONFLICTED)


def test_a_merge_carried_lint_failure_is_repaired_and_the_merge_survives(
    step, tmp_path, monkeypatch
):
    """The whole point of the carried repair: a defect NEITHER side contains is
    fixed in the merge instead of handed to a human. The pass is pointed at the
    carried set alone — the resolved file is not in its write grant — and it is
    told the set is merge-carried, so the prompt names the right defect class."""
    _claude_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv(_LADDER_VARS[0], "tok-live")
    monkeypatch.setenv("BASE_REF", "main")
    _carry_a_clean_change(Path.cwd())
    hook_log = _stub_precommit(
        tmp_path, monkeypatch, 'grep -q "main line" other.md && exit 1\nexit 0'
    )
    home = tmp_path / "repair-scripts"
    home.mkdir(exist_ok=True)
    log = tmp_path / "carried-env.json"
    (home / "repair.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({\n"
        "        'carried': os.environ.get('REPAIR_MERGE_CARRIED', ''),\n"
        "        'files': os.environ['REPAIR_FILE_LIST'].split(),\n"
        "    }) + '\\n')\n"
        "Path('other.md').write_text('repaired carry\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(repair_pass, "_SCRIPT_DIR", home)
    step.read_parents()
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    step.verify_merge_carried_content()
    assert json.loads(log.read_text(encoding="utf-8")) == {
        "carried": "true",
        "files": ["other.md"],
    }
    # The repair rides IN the merge, and the hooks re-judged it rather than
    # trusting the model: reject, re-stage and reject, repair, clean re-run.
    assert "repaired carry" in git_io.git("diff", "--cached", "--", "other.md")
    assert (
        hook_log.read_text(encoding="utf-8").splitlines()
        == ["run --files other.md"] * 3
    )


def test_the_repair_grant_never_carries_a_lockfile(step, tmp_path, monkeypatch):
    """fanout.py refuses a lockfile in the file list, so one in the grant dies on
    every rung identically and the pass reports "produced no usable run" — the
    shape that spent all seven credentials and handed the conflict back. A lock
    command re-derives the file, so dropping it costs the repair nothing. The
    grant narrows and the VERIFY set does not: the hooks still re-run over the
    lockfile, so one that keeps failing still reaches `carried_hook_failures`."""
    _claude_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv(_LADDER_VARS[0], "tok-live")
    (Path.cwd() / CONFLICTED).write_text("broken\n", encoding="utf-8")
    (Path.cwd() / "uv.lock").write_text("stale\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED, "uv.lock")
    step.staged = [CONFLICTED, "uv.lock"]
    hook_log = _stub_precommit(
        tmp_path, monkeypatch, "grep -q broken a.md && exit 1\nexit 0"
    )
    home = tmp_path / "repair-scripts"
    home.mkdir(exist_ok=True)
    log = tmp_path / "grant.json"
    (home / "repair.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        f"with open({str(log)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(os.environ['REPAIR_FILE_LIST'].split()) + '\\n')\n"
        "Path('a.md').write_text('repaired\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(repair_pass, "_SCRIPT_DIR", home)
    assert (
        step.repair_hook_failures(
            tmp_path / "report.txt",
            repairable=[CONFLICTED, "uv.lock"],
            carried=True,
        )
        is True
    )
    assert json.loads(log.read_text(encoding="utf-8")) == [CONFLICTED]
    assert hook_log.read_text(encoding="utf-8").splitlines()[-1] == (
        f"run --files {CONFLICTED} uv.lock"
    )


def test_the_resolver_credential_leads_both_model_ladders(monkeypatch):
    monkeypatch.setenv(_LADDER_VARS[0], "tok-dead")
    monkeypatch.setenv(_LADDER_VARS[2], "tok-live")
    monkeypatch.setenv("RESOLVER_PREFERRED_TOKEN", "tok-live")
    assert bundle.ordered_oauth_tokens() == ["tok-live", "tok-dead"]


def test_a_misconfigured_rung_stops_the_ladder(step, tmp_path, monkeypatch, capsys):
    """Exit 78 says the pass is wired wrong — no CLI, no token, an empty file list —
    which no later credential can fix. Walking on spends every remaining rung on the
    same wall while reporting "produced no usable run", so the wiring failure reads
    as the model being unable to repair the file."""
    _claude_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv(_LADDER_VARS[0], "tok-first")
    monkeypatch.setenv(_LADDER_VARS[1], "tok-second")
    (Path.cwd() / CONFLICTED).write_text("broken\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    _stub_precommit(tmp_path, monkeypatch, "grep -q broken a.md && exit 1\nexit 0")
    log = _stub_repair(tmp_path, monkeypatch, "sys.exit(78)")

    assert step.repair_hook_failures(tmp_path / "report.txt") is False

    assert log.read_text(encoding="utf-8") == "tok-first\n"
    out = capsys.readouterr().out
    assert "::error::" in out
    assert "misconfigured" in out


def test_a_sidecar_only_rejection_never_reaches_the_model(step, tmp_path, monkeypatch):
    """A sidecar path is one the harness refuses to Edit or Write at all, so a
    grant naming it would spend a paid run on a file the run cannot touch."""
    _claude_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv(_LADDER_VARS[0], "tok-primary")
    step.staged = [CONFLICTED]
    step.sidecar = [CONFLICTED]
    log = _stub_repair(tmp_path, monkeypatch, "sys.exit(0)")
    assert step.repair_hook_failures(tmp_path / "report.txt") is False
    assert not log.exists(), "the repair ran on a file the harness refuses to write"


def test_the_repair_grant_excludes_the_sidecar_paths(step, tmp_path, monkeypatch):
    """The set the model may edit is the staged set MINUS the sidecar paths."""
    _claude_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv(_LADDER_VARS[0], "tok-primary")
    (Path.cwd() / CONFLICTED).write_text("resolved\n", encoding="utf-8")
    (Path.cwd() / "other.md").write_text("x\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED, "other.md")
    step.staged = [CONFLICTED, "other.md"]
    step.sidecar = [CONFLICTED]
    _stub_precommit(tmp_path, monkeypatch, "exit 0")
    granted = tmp_path / "granted.txt"
    _stub_repair(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\n"
        f"Path({str(granted)!r}).write_text(os.environ['REPAIR_FILE_LIST'])\n",
    )
    assert step.repair_hook_failures(tmp_path / "report.txt") is True
    assert granted.read_text(encoding="utf-8") == "other.md"


def test_a_dead_ladder_hands_back_the_refusal(step, tmp_path, monkeypatch):
    _claude_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv(_LADDER_VARS[0], "tok-dead")
    step.staged = [CONFLICTED]
    log = _stub_repair(tmp_path, monkeypatch, "sys.exit(1)")
    assert step.repair_hook_failures(tmp_path / "report.txt") is False
    assert log.read_text(encoding="utf-8") == "tok-dead\n"


def test_the_ladder_stops_when_the_pass_runs_out_of_wall_clock(
    step, tmp_path, monkeypatch, capsys
):
    """The whole ladder shares ONE run's budget. Without that, six dead rungs at
    the per-run bound would multiply the repair's cost by six and push the resolve
    job past its own timeout — and a job killed there pushes nothing."""
    _claude_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv(_LADDER_VARS[0], "tok-dead")
    monkeypatch.setenv(_LADDER_VARS[1], "tok-never-reached")
    monkeypatch.setenv("SHARD_TIMEOUT_SECONDS", "1")
    step.staged = [CONFLICTED]
    log = _stub_repair(
        tmp_path, monkeypatch, "import time\ntime.sleep(1.2)\nsys.exit(1)"
    )
    assert step.repair_hook_failures(tmp_path / "report.txt") is False
    assert log.read_text(encoding="utf-8") == "tok-dead\n"
    assert "ran out of its wall-clock budget after 1 of 2" in capsys.readouterr().out


def test_a_marker_free_repair_over_the_merged_tree_stages_and_returns(
    step, tmp_path, monkeypatch, capsys
):
    """The ordinary outcome of the merged-tree pass: it repaired the file and left no
    marker. `git grep` exits 1 on no match, so the undo below it must probe before it
    reads — an unguarded read turns every SUCCESSFUL repair into a bare exit 1, and this
    call site is the one the tests elsewhere stub out."""
    _claude_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv(_LADDER_VARS[0], "tok-primary")
    (Path.cwd() / CONFLICTED).write_text("resolved\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    # The merged-tree grant is the staged set plus what the merge carried, and that
    # second half reads both parents — which the step learns from the merge in progress.
    step.read_parents()
    step.staged = [CONFLICTED]
    _stub_repair(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\n"
        "Path('a.md').write_text('repaired\\n', encoding='utf-8')\n",
    )

    assert (
        step.repair_merged_tree(tmp_path / "report.txt", "the post-merge check") is True
    )
    assert CONFLICTED in git_io.git_lines("diff", "--cached", "--name-only")
    out = capsys.readouterr().out
    assert "put back the repair pass's edit(s)" not in out, out


def test_a_repair_that_writes_markers_has_that_edit_put_back(
    step, tmp_path, monkeypatch, capsys
):
    """A repair that leaves conflict markers made the tree worse than the content
    it was fixing, so that edit goes back and the reader that rejected the tree
    keeps its own finding — a marker this pass wrote never reaches a human."""
    _claude_on_path(tmp_path, monkeypatch)
    _stub_gh(tmp_path, monkeypatch)
    # The undo lets the pass carry on to the hook re-run, which needs the binary
    # on PATH. Without this stub the test reads whatever the runner happens to
    # have installed, which is how it passed here and failed on CI.
    _stub_precommit(tmp_path, monkeypatch, "exit 0")
    monkeypatch.setenv(_LADDER_VARS[0], "tok-primary")
    (Path.cwd() / CONFLICTED).write_text("broken\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    marker = "<" * 7
    _stub_repair(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\n"
        f"Path('a.md').write_text('{marker} HEAD\\nx\\n', encoding='utf-8')\n",
    )
    step.repair_hook_failures(tmp_path / "report.txt")
    assert (Path.cwd() / CONFLICTED).read_text(encoding="utf-8") == "broken\n"
    out = capsys.readouterr().out
    # The runner's tree is gone by the time a human reads the run, so the file
    # and line are the only record of where the markers landed.
    assert "Conflict markers reintroduced by the hook-repair pass:" in out
    assert f"{CONFLICTED}:1:{marker} HEAD" in out
    assert "put back the repair pass's edit(s)" in out


@pytest.mark.parametrize("raw", ["0", "²", "٣٠"])
def test_a_malformed_repair_budget_is_refused_not_defaulted(
    tmp_path, monkeypatch, capsys, raw
):
    """The bound is what keeps the ladder inside the resolve job's timeout, so a
    value that is set but unusable must red rather than silently widen to the
    default — the fan-out reader dies on the same value, and this reader also
    runs on the deterministic-only path where the fan-out never ran.

    The Unicode digits reach the refusal only because it matches ASCII digits:
    str.isdigit() calls them digits, so a bare check let them reach int() and the
    step died with a traceback instead of the sentence below.
    """
    # The refusal aborts the merge on its way out, so this test needs a repository
    # of its own — unbound, that abort would reach the tree pytest is running in.
    _enter_repo(_repo(tmp_path), monkeypatch)
    _stub_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("PR", "1")
    monkeypatch.setenv("SHARD_TIMEOUT_SECONDS", raw)
    with pytest.raises(SystemExit) as raised:
        hook_gate.shard_timeout_seconds()
    assert raised.value.code == 1
    out = capsys.readouterr().out
    assert f"must be a positive whole number of seconds, got '{raw}'" in out


def test_the_repair_budget_honours_the_configured_value(monkeypatch):
    """Unset is the one value that falls back, so the fallback cannot swallow a
    configured bound."""
    monkeypatch.setenv("SHARD_TIMEOUT_SECONDS", "420")
    assert hook_gate.shard_timeout_seconds() == 420
    monkeypatch.delenv("SHARD_TIMEOUT_SECONDS")
    assert hook_gate.shard_timeout_seconds() > 0


def test_a_repair_the_hooks_then_AUTO_FIX_still_bundles(step, tmp_path, monkeypatch):
    """The post-repair contract is fix-then-verify, not pass-or-refuse: a repair
    that fixes the named error can still leave a nit a formatter hook rewrites,
    and that rewrite has to be staged and re-judged rather than refused."""
    _claude_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv(_LADDER_VARS[0], "tok-primary")
    (Path.cwd() / CONFLICTED).write_text("broken\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    # Rewrites the file and exits 1 the first time (pre-commit's "files were
    # modified by this hook"), passes the second.
    _stub_precommit(
        tmp_path,
        monkeypatch,
        "if [[ -e .git/pc-ran ]]; then exit 0; fi\n"
        ": >.git/pc-ran\n"
        "printf 'reformatted\\n' > a.md\n"
        "exit 1",
    )
    _stub_repair(
        tmp_path,
        monkeypatch,
        "from pathlib import Path\n"
        "Path('a.md').write_text('repaired\\n', encoding='utf-8')\n",
    )
    assert step.repair_hook_failures(tmp_path / "report.txt") is True
    assert git_io.git("show", ":a.md") == "reformatted\n"


def test_a_repair_that_still_fails_the_hooks_reports_false(step, tmp_path, monkeypatch):
    _claude_on_path(tmp_path, monkeypatch)
    monkeypatch.setenv(_LADDER_VARS[0], "tok-primary")
    (Path.cwd() / CONFLICTED).write_text("still broken\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    _stub_precommit(tmp_path, monkeypatch, 'echo "ruff.....Failed"; exit 1')
    _stub_repair(tmp_path, monkeypatch, "sys.exit(0)")
    assert step.repair_hook_failures(tmp_path / "report.txt") is False


# --- committing, the self-review gate, and the bundle ------------------------


def test_the_open_merge_is_committed(step):
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.commit_the_merge()
    assert git_io.git_status("rev-parse", "-q", "--verify", "MERGE_HEAD") != 0
    assert len(git_io.git("rev-list", "--parents", "-n", "1", "HEAD").split()) == 3


def test_a_closed_merge_with_nothing_staged_is_left_alone(step):
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.commit_the_merge()
    before = git_io.git("rev-parse", "HEAD").strip()
    step.commit_the_merge()
    assert git_io.git("rev-parse", "HEAD").strip() == before


def test_content_left_staged_after_the_merge_is_folded_into_it(step):
    """Should anything ever leave content staged here, it belongs IN the merge
    commit, which was never pushed, so amending keeps those bytes out of a
    separate commit no resolution authored."""
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.commit_the_merge()
    before = git_io.git("rev-parse", "HEAD").strip()
    (Path.cwd() / CONFLICTED).write_text("amended\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.commit_the_merge()
    assert git_io.git("rev-parse", "HEAD").strip() != before
    assert len(git_io.git("rev-list", "--count", "HEAD").split()) == 1


def test_the_self_review_gate_is_skipped_without_a_credential(
    step, monkeypatch, capsys
):
    """The review is ON by default, so this skip is the path an adopter who
    configured no credential takes on every resolve. It must say so: a silent
    return reads as a review that ran and found nothing."""
    monkeypatch.setenv("AUTO_RESOLVE_SELF_REVIEW", "true")
    for name in _LADDER_VARS:
        monkeypatch.delenv(name, raising=False)
    step.run_self_review()
    assert "::warning::self-review skipped" in capsys.readouterr().out
    # The marker is what stops reuse-bundle.py refusing this bundle forever: a
    # later run for the same head reaches this same branch, so a bundle carrying
    # neither marker could never be produced and the resolve is re-bought.
    assert step.unverified is True


def test_the_self_review_runs_on_every_spelling_but_the_caller_s_opt_out(
    step, tmp_path, monkeypatch, capsys
):
    """The gate compares against the workflow's own rendering of `self-review: true`,
    so every other spelling skips — even with a credential configured and a reviewer
    that would refuse: a stub that exits 1 must never run."""
    _committed_merge(step)
    _stub_self_review(tmp_path, monkeypatch, "exit 1")
    for spelling in (None, "", "false", "True", "1"):
        if spelling is None:
            monkeypatch.delenv("AUTO_RESOLVE_SELF_REVIEW", raising=False)
        else:
            monkeypatch.setenv("AUTO_RESOLVE_SELF_REVIEW", spelling)
        step.run_self_review()
        assert "the caller turned it off" in capsys.readouterr().out, repr(spelling)


def test_the_declared_default_renders_to_the_spelling_the_gate_accepts():
    """The whole behavioural change is one token in a file this process never runs,
    and the gate above compares against a STRING — so a default quoted into
    `"true"`, or flipped back to `false`, would pass every test that only drives the
    environment variable. Pin the declared default and the accepted spelling
    together: no single process observes both."""
    declared = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "auto-resolve.yaml").read_text(
            encoding="utf-8"
        )
    )[True]["workflow_call"]["inputs"]["self-review"]
    assert declared["type"] == "boolean"
    assert declared["default"] is True, (
        "a caller that names no self-review input must get the review; a merge "
        "the resolver pushes unread is the case this default exists to prevent"
    )
    # A boolean input reaches the step as the lowercase string the gate tests for.
    assert str(declared["default"]).lower() == "true"


def _stub_self_review(tmp_path, monkeypatch, body: str) -> None:
    """The reviewer is a sibling script the step runs by path, so pointing the
    directory at a stub keeps this suite off a paid model run."""
    home = tmp_path / "reviewer"
    home.mkdir(exist_ok=True)
    script = home / "self_review.py"
    # BODY stays shell: it stands in for the reviewer's OUTCOME, and the outcomes
    # this suite scripts (an exit status, a line of output, an amend) are one shell
    # line each. The wrapper is what the step now runs by path.
    script.write_text(
        "#!/usr/bin/env python3\nimport subprocess, sys\n"
        f"sys.exit(subprocess.run(['bash', '-c', {body!r}], check=False).returncode)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setattr(self_review_gate, "_SCRIPT_DIR", home)
    monkeypatch.setenv(_LADDER_VARS[0], "a-credential")
    # The review is opt-in, and every test driving the stub is a test of a review
    # that RUNS.
    monkeypatch.setenv("AUTO_RESOLVE_SELF_REVIEW", "true")


def _committed_merge(step) -> None:
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.commit_the_merge()


def test_a_clean_self_review_leaves_the_merge_alone(
    step, tmp_path, monkeypatch, capsys
):
    _committed_merge(step)
    before = git_io.git("rev-parse", "HEAD").strip()
    _stub_self_review(
        tmp_path,
        monkeypatch,
        'test "$SELF_REVIEW_TOKEN_LADDER" = "a-credential"\n'
        'echo "no suspicious deltas"',
    )
    step.run_self_review()
    assert "no suspicious deltas" in capsys.readouterr().out
    assert git_io.git("rev-parse", "HEAD").strip() == before


def test_the_reviewer_learns_the_pre_pass_already_verified(step, tmp_path, monkeypatch):
    """Both regeneration flags reach the reviewer as "true" exactly when a
    pre-pass is declared AND a fresh `--verify` passes at the commit the
    renderer reads, so it may retire the caller's rule-owned outputs without
    re-deriving them in a bare worktree."""
    _committed_merge(step)
    monkeypatch.setattr(bundle._pre_pass, "PRE_PASS", ["pnpm", "resolve-generated"])
    monkeypatch.setattr(
        bundle._pre_pass,
        "run_pre_pass",
        lambda *args: subprocess.CompletedProcess(args, 0, "", ""),
    )
    _stub_self_review(
        tmp_path,
        monkeypatch,
        'test "$AUTO_RESOLVE_VERIFY_REGENERATED" = "true"\n'
        'test "$AUTO_RESOLVE_PRE_PASS_VERIFIED" = "true"',
    )
    step.run_self_review()


def test_a_post_verify_rewrite_drops_the_pre_pass_claim(step, tmp_path, monkeypatch):
    """The hook and repair passes run AFTER verify_generated_artifacts and can
    rewrite a generated file, so the claim is re-proved at the commit the
    renderer reads: a `--verify` that fails there reaches the reviewer as
    "false", and the renderer re-derives in its own scratch worktree."""
    _committed_merge(step)
    monkeypatch.setattr(bundle._pre_pass, "PRE_PASS", ["pnpm", "resolve-generated"])
    monkeypatch.setattr(
        bundle._pre_pass,
        "run_pre_pass",
        lambda *args: subprocess.CompletedProcess(args, 1, "", ""),
    )
    _stub_self_review(
        tmp_path,
        monkeypatch,
        'test "$AUTO_RESOLVE_VERIFY_REGENERATED" = "true"\n'
        'test "$AUTO_RESOLVE_PRE_PASS_VERIFIED" = "false"',
    )
    step.run_self_review()


def test_a_reviewer_that_could_not_verify_lands_the_resolution_flagged(
    step, tmp_path, monkeypatch, capsys
):
    """A reviewer outage says nothing about the resolution, so discarding one spends
    the whole fan-out to punish a rate-limited credential and leaves the conflict for
    the next scan to buy again."""
    _committed_merge(step)
    _stub_self_review(tmp_path, monkeypatch, "exit 2")
    step.run_self_review()
    assert step.unverified is True
    assert "UNVERIFIED" in capsys.readouterr().out


def test_an_unverified_resolution_tells_land_to_hold_it_for_a_human(
    step, tmp_path, monkeypatch
):
    _committed_merge(step)
    step.read_parents()
    _stub_self_review(tmp_path, monkeypatch, "exit 2")
    step.run_self_review()
    step.write_the_bundle()
    assert (step.bundle_dir / "unverified").is_file()


def test_a_verified_resolution_leaves_no_unverified_marker(step, tmp_path, monkeypatch):
    _committed_merge(step)
    step.read_parents()
    _stub_self_review(tmp_path, monkeypatch, 'echo "no suspicious deltas"')
    step.run_self_review()
    step.write_the_bundle()
    assert not (step.bundle_dir / "unverified").exists()


def test_a_reviewer_verdict_against_the_resolution_refuses_the_bundle(
    step, tmp_path, monkeypatch, capsys
):
    _committed_merge(step)
    _stub_self_review(
        tmp_path, monkeypatch, 'echo "traceable to neither parent"; exit 1'
    )
    with pytest.raises(SystemExit):
        step.run_self_review()
    assert "flagged by the merge-delta reviewer" in capsys.readouterr().out


def test_a_refused_resolution_keeps_the_reviewer_s_findings(
    step, tmp_path, monkeypatch
):
    """Both records this refusal leaves are erased: the run log ages out, and the
    sticky comment is one per pull request, so the next run overwrites it. Without
    this the findings survive nowhere and nobody can act on what the reviewer
    refused."""
    _committed_merge(step)
    _stub_gh(tmp_path, monkeypatch)
    _stub_self_review(
        tmp_path,
        monkeypatch,
        'mkdir -p "$SELF_REVIEW_DIR"; '
        'echo "- uv.lock:936 — untraced hunks" >"$SELF_REVIEW_DIR/merge-review.md"; '
        "exit 1",
    )
    with pytest.raises(SystemExit):
        step.run_self_review()
    kept = Path(os.environ["BUNDLE_DIR"]) / "merge-review.md"
    assert "untraced hunks" in kept.read_text(encoding="utf-8")
    comment = status_comments((tmp_path / "gh.log").read_text(encoding="utf-8"))[0]
    assert "untraced hunks" in comment


def test_a_flagged_resolution_no_round_corrected_says_which_budget_went(
    step, tmp_path, monkeypatch, capsys
):
    """Exit 3 is a flagged resolution NO fix round ran against, because none fit
    in the wall clock left. Telling the pull request that "the automatic correction
    could not satisfy the reviewer" is false there: it describes a correction that
    never happened, and sends the reader at the merge instead of at the budget."""
    _committed_merge(step)
    _stub_self_review(
        tmp_path, monkeypatch, 'echo "traceable to neither parent"; exit 3'
    )
    with pytest.raises(SystemExit):
        step.run_self_review()
    said = capsys.readouterr().out
    assert "no fix round fit in its wall-clock budget" in said
    assert "could not satisfy the reviewer" not in said


def test_the_fixers_own_bytes_go_back_through_the_lint_gate(
    step, tmp_path, monkeypatch
):
    """A self-review that AMENDED put machine-authored bytes into the commit after
    the lint gate already passed, so those bytes have been through no hook at
    all."""
    _committed_merge(step)
    # main() reads the parents before anything else, and the post-fixer passes
    # compare the fixer's tree against the mechanical merge of those two.
    step.read_parents()
    step.staged = [CONFLICTED]
    _stub_self_review(
        tmp_path,
        monkeypatch,
        'printf "fixed\\n" > a.md\n'
        "git add -- a.md\n"
        "git commit --amend --no-edit --no-verify -q\n",
    )
    linted = tmp_path / "linted.txt"
    _stub_precommit(tmp_path, monkeypatch, f'printf "%s\\n" "$*" >>"{linted}"\nexit 0')
    step.run_self_review()
    assert CONFLICTED in linted.read_text(encoding="utf-8")


def test_the_fixers_auto_fixes_are_folded_back_into_the_merge(
    step, tmp_path, monkeypatch
):
    _committed_merge(step)
    step.read_parents()
    step.staged = [CONFLICTED]
    _stub_self_review(
        tmp_path,
        monkeypatch,
        'printf "fixed\\n" > a.md\n'
        "git add -- a.md\n"
        "git commit --amend --no-edit --no-verify -q\n",
    )
    # The hook rewrites the file on its first run and passes on its second, which
    # leaves content staged that must land IN the merge commit, not beside it.
    _stub_precommit(
        tmp_path,
        monkeypatch,
        'if [ ! -f .ran ]; then touch .ran; printf "hooked\\n" > a.md; git add -- a.md; exit 1; fi\nexit 0',
    )
    step.run_self_review()
    assert git_io.git("show", "HEAD:a.md") == "hooked\n"
    assert len(git_io.git("rev-list", "--count", "HEAD").split()) == 1


# --- the caller's setup command, which the merge commit must not carry --------


def _prepared(tmp_path, monkeypatch, command) -> None:
    """Run COMMAND the way the workflow's setup step does — sampled either side,
    so the record names exactly what it changed."""
    monkeypatch.setenv("AUTO_RESOLVE_SETUP_RECORD", str(tmp_path / "setup.json"))
    git_io.bind_repo(Path.cwd())
    setup_record.capture_before()
    command()
    setup_record.capture_after()


def _bundle_a_resolution(tmp_path, monkeypatch) -> None:
    """Resolve the one conflict and drive the whole step, as the job does."""
    monkeypatch.setenv("HEAD_REF", "feature")
    _stub_precommit(tmp_path, monkeypatch, "exit 0")
    _stub_pnpm(tmp_path, monkeypatch, "exit 0")
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    bundle.main()


def test_a_setup_command_that_deletes_a_tracked_file_still_bundles(
    step, tmp_path, monkeypatch
):
    """The motivating case, end to end. `.dotfiles` prunes `.claude/hooks`, which
    is TRACKED, so without the undo the deletion reads as an edit outside the
    conflicted set and the whole paid resolution is refused and blamed on the
    model."""
    tracked = Path.cwd() / "untouched.md"
    _prepared(tmp_path, monkeypatch, tracked.unlink)
    _bundle_a_resolution(tmp_path, monkeypatch)
    assert (tmp_path / "bundle" / "merge.bundle").exists()
    assert tracked.read_text(encoding="utf-8") == "base\n", (
        "the repair reached the merge commit; it is not part of the resolution"
    )


def test_a_setup_command_that_creates_a_file_still_bundles(step, tmp_path, monkeypatch):
    """The untracked arm of the same refusal, which a setup command that writes a
    cache or a generated helper hits."""
    made = Path.cwd() / "prepared.txt"
    _prepared(tmp_path, monkeypatch, lambda: made.write_text("x", encoding="utf-8"))
    _bundle_a_resolution(tmp_path, monkeypatch)
    assert (tmp_path / "bundle" / "merge.bundle").exists()
    assert not made.exists()


def test_a_setup_path_the_model_then_edited_still_aborts(step, tmp_path, monkeypatch):
    """INVARIANT — the undo must not become a way to smuggle a model edit past the
    outside-the-set check. The record holds what the command LEFT, so a later
    change to the same path is a model edit wearing a setup change's clothes."""
    touched = Path.cwd() / "untouched.md"
    _prepared(
        tmp_path, monkeypatch, lambda: touched.write_text("setup\n", encoding="utf-8")
    )
    touched.write_text("the model was here\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        _bundle_a_resolution(tmp_path, monkeypatch)


def test_a_setup_command_that_rewrites_a_conflicted_file_is_refused(
    step, tmp_path, monkeypatch
):
    """Refused at the SAMPLE, before any model call: the model resolves that same
    file next, so nothing downstream could separate the two edits."""
    monkeypatch.setenv("AUTO_RESOLVE_SETUP_RECORD", str(tmp_path / "setup.json"))
    git_io.bind_repo(Path.cwd())
    setup_record.capture_before()
    (Path.cwd() / CONFLICTED).write_text("setup rewrote the conflict\n", "utf-8")
    with pytest.raises(SystemExit):
        setup_record.capture_after()


def test_no_setup_command_leaves_the_tree_alone(step, monkeypatch):
    """A caller that names none writes no record, and the undo is then a no-op
    rather than a read of whatever file the variable happens to point at."""
    monkeypatch.delenv("AUTO_RESOLVE_SETUP_RECORD", raising=False)
    (Path.cwd() / "untouched.md").unlink()
    setup_record.undo_setup_changes()
    assert not (Path.cwd() / "untouched.md").exists()


# --- the plumbing every check above is built on -------------------------------


def test_a_failing_git_command_exits_with_its_status(step, capsys):
    with pytest.raises(SystemExit):
        git_io.git("rev-parse", "--verify", "no-such-ref")
    assert "Needed a single revision" in capsys.readouterr().err


def test_an_abort_that_itself_fails_warns_and_leaves_the_tree(monkeypatch, capsys):
    """The checkout is discarded either way, so a failed abort is a warning and
    never the run's verdict."""
    monkeypatch.setattr(
        git_io, "git_status", lambda *args: 0 if args[0] == "rev-parse" else 1
    )
    git_io.abort_merge_if_in_progress()
    assert "::warning::git merge --abort failed" in capsys.readouterr().err


def test_a_missing_required_input_is_refused_before_any_git_call(step, monkeypatch):
    monkeypatch.delenv("PR", raising=False)
    with pytest.raises(SystemExit):
        bundle.main()


def test_the_whole_step_bundles_a_resolved_merge(step, tmp_path, monkeypatch):
    """The one end-to-end pass through `main`, which is the order the checks run
    in — the corpus pins the bytes it prints, this pins that the order works."""
    monkeypatch.setenv("HEAD_REF", "feature")
    monkeypatch.setenv("BASE_REF", "main")
    for name in _LADDER_VARS:
        monkeypatch.delenv(name, raising=False)
    _stub_precommit(tmp_path, monkeypatch, "exit 0")
    _stub_pnpm(tmp_path, monkeypatch, "exit 0")
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    bundle.main()
    assert (tmp_path / "bundle" / "merge.bundle").exists()


def test_the_bundle_carries_the_result_ref(step, tmp_path):
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.read_parents()
    step.commit_the_merge()
    step.write_the_bundle()
    heads = subprocess.run(
        ["git", "bundle", "list-heads", str(tmp_path / "bundle" / "merge.bundle")],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path.cwd(),
    ).stdout
    assert bundle.AUTO_RESOLVE_RESULT_REF in heads


def test_the_bundle_records_the_head_the_reuse_probe_compares(step, tmp_path):
    """reuse-bundle.py answers `hit` by comparing parents.json's `head` to the
    branch's current head — a record of the wrong parent would let a later run
    reuse a resolution built for a head the branch no longer holds."""
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.read_parents()
    step.commit_the_merge()
    step.write_the_bundle()
    parents = json.loads(
        (tmp_path / "bundle" / "parents.json").read_text(encoding="utf-8")
    )
    assert parents == {
        "head": git_io.git("rev-parse", "HEAD^").strip(),
        "base": git_io.git("rev-parse", "HEAD^2").strip(),
    }


def test_an_unresolved_conflict_outside_the_named_set_stops_the_bundle(
    step, tmp_path, monkeypatch, capsys
):
    """Every conflicted path is either staged from the resolution or regenerated,
    so anything git still reports unmerged was never resolved at all — and it
    carries no marker to be diagnosed by the sweep above."""
    monkeypatch.setenv("HEAD_REF", "feature")
    monkeypatch.setenv("BASE_REF", "main")
    _stub_pnpm(tmp_path, monkeypatch, "exit 0")
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    _leave_unmerged("other.md")
    with pytest.raises(SystemExit):
        bundle.main()
    assert "unmerged paths remain after staging" in capsys.readouterr().out


# ── the hooks the resolver must not run ───────────────────────────────────────


def test_project_env_hooks_are_read_from_the_config(tmp_path: Path) -> None:
    """Only the `uv run` entries come back: those are the ones that would resolve
    the project environment from a workspace the pull request controls."""
    config = tmp_path / ".pre-commit-config.yaml"
    config.write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: uses-the-project-env\n"
        "        entry: uv run pytest tests/test_x.py -q\n"
        "      - id: also-uses-it\n"
        "        entry: \"bash -c 'uv run python scripts/gen-x.py && git add out'\"\n"
        "      - id: plain-system-hook\n"
        "        entry: python3 .github/scripts/checks/x.py\n"
        "      - id: no-entry-at-all\n",
        encoding="utf-8",
    )
    assert hook_gate.hooks_needing_the_project_env(config) == [
        "also-uses-it",
        "uses-the-project-env",
    ]


def test_every_uv_run_hook_in_the_real_config_is_refused() -> None:
    """Driven against the committed config, so a new `uv run` hook is covered the
    day it lands rather than the day somebody remembers to list it."""
    config = REPO_ROOT / ".pre-commit-config.yaml"
    refused = hook_gate.hooks_needing_the_project_env(config)
    assert refused, "no `uv run` hooks found — has the config schema changed?"
    doc = yaml.safe_load(config.read_text(encoding="utf-8"))
    entries = {
        hook["id"]: hook.get("entry", "")
        for repo in doc["repos"]
        for hook in repo["hooks"]
    }
    assert all("uv run" in entries[hook_id] for hook_id in refused)
    assert not [
        hook_id
        for hook_id, entry in entries.items()
        if "uv run" in entry and hook_id not in refused
    ]


def test_the_uv_run_hooks_are_refused_through_the_skip_env(step, tmp_path, monkeypatch):
    """The wiring, not the derivation: the hook ids must reach pre-commit as SKIP,
    which is the whole mechanism keeping a `uv run` out of this job."""
    skip_log = tmp_path / "skip.log"
    _stub_precommit(tmp_path, monkeypatch, f'printf "%s" "$SKIP" >"{skip_log}"\nexit 0')
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    step.verify_resolved_content()
    assert skip_log.read_text(encoding="utf-8") == "resolves-the-project-env"


def test_no_config_means_nothing_to_refuse(tmp_path: Path) -> None:
    """`pre-commit run` finds no hooks either without a config, so the empty set
    is accurate rather than a bypass."""
    assert hook_gate.hooks_needing_the_project_env(tmp_path / "absent.yaml") == []


# ── whether a push superseded the commit this run read ───────────────────────


def _merge_commit_repo(tmp_path: Path) -> Path:
    """A repository whose HEAD is itself a merge commit, as a PR head often is.

    Every auto-resolve land, and every `git merge origin/main` a session runs on
    its own branch, leaves the pull request's head in exactly this shape.
    """
    work = tmp_path / "merged"
    work.mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@e")
    (work / "a.md").write_text("base\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "checkout", "-q", "-b", "side")
    (work / "b.md").write_text("side\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "side")
    _git(work, "checkout", "-q", "main")
    (work / "c.md").write_text("main\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "main")
    _git(work, "merge", "-q", "--no-edit", "side")
    return work


@pytest.fixture
def head_server(tmp_path, monkeypatch):
    """A real GitHub serving the PR's live head, read by the real `gh`."""
    server = FakeHeadRuns(tmp_path)
    with server:
        for name, value in server.env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("GH_REPO", server.repo)
        monkeypatch.setenv("PR", "4043")
        yield server


def test_a_head_that_is_itself_a_merge_commit_is_not_read_as_a_push(
    head_server, tmp_path, monkeypatch
):
    """The regression (run 31574164227, PR 3970): the head was a merge commit,
    so deriving the starting point from the local `HEAD^` named that merge's
    first parent — and the live head, unchanged, compared unequal to it. The
    refusal comment was dropped for a push nobody made."""
    _enter_repo(_merge_commit_repo(tmp_path), monkeypatch)
    monkeypatch.setenv("HEAD_SHA", head_server.live_head)
    assert refusal.superseding_head() == ""


def test_a_push_that_really_moved_the_head_reports_the_new_commit(
    head_server, monkeypatch
):
    monkeypatch.setenv("HEAD_SHA", "c" * 40)
    assert refusal.superseding_head() == head_server.live_head


def test_a_refused_head_read_leaves_the_refusal_comment_in_place(
    head_server, monkeypatch
):
    """No answer is not evidence of a push. `gh` prints the error BODY on stdout,
    so a caller matching anything non-empty would read a 403 message as a SHA."""
    head_server.pull_status = 403
    monkeypatch.setenv("HEAD_SHA", "d" * 40)
    assert refusal.superseding_head() == ""


# --- one declined path must not discard every resolved one -------------------


def _declined_fixture(
    tmp_path, monkeypatch, declined=("b.md",), main_extra=None, **env
) -> "bundle.Bundle":
    """A tree where one conflicted path resolved and the other was DECLINED.

    The decline record is what makes `b.md` a decline rather than a shard that
    answered nothing, and only a decline is salvaged."""
    step = _with_second_path(
        tmp_path, monkeypatch, main_extra=main_extra, BASE_REF="main", **env
    )
    _execution_log(
        tmp_path,
        monkeypatch,
        [
            {
                "file": name,
                "resolved": name not in declined,
                "is_error": False,
                "declined": name in declined,
                "decline_reason": "the two sides disagree on intent",
            }
            for name in (CONFLICTED, "b.md")
        ],
    )
    step.read_parents()
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    (Path.cwd() / "b.md").write_text(
        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> main\n", encoding="utf-8"
    )
    step.stage_text_resolutions()
    return step


def test_one_declined_path_does_not_discard_the_paths_that_resolved(
    tmp_path, monkeypatch
):
    """The regression: a whole-tree marker check over per-path work threw away 19
    resolved files because the 20th kept its markers, and the next scan bought the
    identical resolution again."""
    step = _declined_fixture(tmp_path, monkeypatch)
    step.salvage_declined_paths()
    assert step.declined == ["b.md"]
    # The declined path carries this branch's content, not the markers.
    assert "<<<<<<<" not in Path("b.md").read_text(encoding="utf-8")
    # And the whole-tree post-condition now passes, so the resolution reaches land.
    step.marker_verdict().refuse_leftover_markers(".")


def test_a_run_whose_every_path_declined_still_refuses(tmp_path, monkeypatch):
    """Salvaging nothing is a refusal: there is no resolution left to land."""
    step = _declined_fixture(tmp_path, monkeypatch, declined=(CONFLICTED, "b.md"))
    Path(CONFLICTED).write_text(
        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> main\n", encoding="utf-8"
    )
    step.salvage_declined_paths()
    assert step.declined == []
    with pytest.raises(SystemExit):
        step.marker_verdict().refuse_leftover_markers(".")


def test_a_decline_that_would_revert_the_base_is_not_salvaged(
    tmp_path, monkeypatch, capsys
):
    """`b.md` here is edited on the base side only, so this branch's content equals
    the merge base: keeping it undoes the base's landed commit rather than choosing
    between two edits. The markers stay, so the leftover-marker verdict refuses."""
    step = _declined_fixture(tmp_path, monkeypatch, main_extra={"b.md": "main b\n"})
    step.salvage_declined_paths()
    assert step.declined == []
    assert "<<<<<<<" in Path("b.md").read_text(encoding="utf-8")
    assert "REVERT" in capsys.readouterr().out


def test_a_path_neither_side_edited_is_not_called_a_revert(tmp_path, monkeypatch):
    """The other direction: this branch's content equals the merge base, but so does
    the base side's, so there is no landed change for the decline to undo. Reading
    only the head against the base refused this correct salvage."""
    step = _declined_fixture(tmp_path, monkeypatch)
    assert step.keeping_head_reverts_the_base("b.md") is False


def test_a_permission_denial_is_never_salvaged(tmp_path, monkeypatch):
    """A closed write path means the base's edit would be dropped over a fixable
    grant, so the markers stay and the denial diagnosis runs as it does today."""
    step = _declined_fixture(
        tmp_path,
        monkeypatch,
        LLM_PERMISSION_DENIALS="2",
        LLM_PERMISSION_DENIED_TOOLS="Edit",
    )
    step.salvage_declined_paths()
    assert step.declined == []
    assert "<<<<<<<" in Path("b.md").read_text(encoding="utf-8")


def test_a_declined_path_is_named_to_land(tmp_path, monkeypatch):
    step = _declined_fixture(tmp_path, monkeypatch)
    step.salvage_declined_paths()
    step.commit_the_merge()
    step.write_the_bundle()
    assert (step.bundle_dir / "declined").read_text(encoding="utf-8") == "b.md\n"


def test_importing_the_step_under_a_captured_stdout_does_not_raise(monkeypatch):
    """A harness can replace `sys.stdout` with a capture object that has no
    `reconfigure`, and an unguarded call raises AttributeError before any test body
    runs. Re-executed in THIS interpreter, not a child: coverage traces no subprocess,
    so a child run leaves the guard's skipped arm unmeasured."""
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    load_script(".github/resolver/auto-resolve/bundle.py")


def test_importing_the_step_turns_on_line_buffering_for_a_real_stream(monkeypatch):
    """The other arm, and the one the guard exists to reach: a real text stream gets
    `line_buffering`, so a print() here flushes at the newline instead of sitting in a
    buffer while an inherited-stdout child writes through the same fd."""
    stream = io.TextIOWrapper(io.BytesIO(), line_buffering=False)
    monkeypatch.setattr(sys, "stdout", stream)
    load_script(".github/resolver/auto-resolve/bundle.py")
    assert stream.line_buffering


def test_a_fork_head_runs_no_repo_hook_at_all(step, tmp_path, monkeypatch):
    """The untrusted-head boundary, from this side. A fork head's `.pre-commit-config`
    hooks are code the fork's author wrote, and this job holds every model credential
    — so the resolve job installs no hook toolchain and this pass must not try to run
    one. The pull request's own required checks judge the merged bytes instead."""
    log = _stub_precommit(tmp_path, monkeypatch, "exit 0")
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    monkeypatch.setenv("AUTO_RESOLVE_UNTRUSTED_HEAD", "true")
    # The carried set too, which the merge would otherwise hand the same hooks.
    monkeypatch.setattr(step, "merge_carried_paths", lambda: [CONFLICTED])
    step.verify_resolved_content()
    step.verify_merge_carried_content()
    assert not log.exists()


def test_a_same_repo_head_still_runs_the_hooks(step, tmp_path, monkeypatch):
    """The other direction, so the skip cannot widen silently: anything but the exact
    string leaves the hook pass enforcing."""
    log = _stub_precommit(tmp_path, monkeypatch, "exit 0")
    (Path.cwd() / CONFLICTED).write_text("merged\n", encoding="utf-8")
    git_io.git("add", "--", CONFLICTED)
    step.staged = [CONFLICTED]
    monkeypatch.setenv("AUTO_RESOLVE_UNTRUSTED_HEAD", "false")
    step.verify_resolved_content()
    assert log.read_text(encoding="utf-8").strip() == f"run --files {CONFLICTED}"


# An interpreter that died unwinding its own imports. `uv run --no-project`
# exits 1 for this, which is what a verifier reporting a stale artifact exits.
_PRE_PASS_CRASH = (
    'echo "Traceback (most recent call last):" >&2;'
    ' echo "  File \\"rules.py\\", line 1, in <module>" >&2;'
    " echo \"ModuleNotFoundError: No module named 'yaml'\" >&2; exit 1"
)


def test_a_pre_pass_that_CRASHED_is_named_as_plumbing_not_a_stale_artifact(
    step, tmp_path, monkeypatch, capsys
):
    """agent-glovebox #5521: one generated-file rule left `pyyaml` off its own
    interpreter pins, the verifier died importing `yaml`, and the run told a
    human the resolution held bytes no build produces — over a file no conflict
    touched. A command that crashed reached no verdict, so nothing here may
    blame the branch, and the head takes no mark: a re-run after the pin lands
    resolves this same head."""
    _stub_pnpm(tmp_path, monkeypatch, _PRE_PASS_CRASH)
    _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        step.verify_generated_artifacts()
    assert "could not RUN" in capsys.readouterr().out
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "defect in this workflow's provisioning" in log
    assert "auto-resolve/handed-off" not in log


def test_the_same_pre_pass_crash_stops_the_deferred_re_derivation(
    tmp_path, monkeypatch, capsys
):
    """The other call site, one step earlier, reads the same crash the same way."""
    step = _with_second_path(tmp_path, monkeypatch, DEFERRED_REGEN="b.md")
    _stub_pnpm(tmp_path, monkeypatch, _PRE_PASS_CRASH)
    _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        step.run_deferred_regeneration()
    assert "could not RUN" in capsys.readouterr().out
    assert "auto-resolve/handed-off" not in (tmp_path / "gh.log").read_text(
        encoding="utf-8"
    )


def test_a_crashed_pre_pass_is_plumbing_even_when_it_leaves_deferred_paths_unmerged(
    tmp_path, monkeypatch, capsys
):
    """The shape #5521 actually had: a crashed pre-pass never re-derives its
    deferred paths, so they are still UNMERGED when control reaches
    `_deferred_unmerged`. That check must not fire first — it would blame the
    branch for bytes that never regenerated because the tool never ran."""
    step = _two_conflict_step(tmp_path, monkeypatch)
    monkeypatch.setenv("DEFERRED_REGEN", "b.md")
    _stub_pnpm(tmp_path, monkeypatch, _PRE_PASS_CRASH)
    _stub_gh(tmp_path, monkeypatch)
    assert git_io.git_lines("ls-files", "-u", "--", "b.md"), (
        "the fixture must leave b.md genuinely unmerged, or this test cannot "
        "tell #5521's shape from the one _with_second_path already covers"
    )
    with pytest.raises(SystemExit):
        step.run_deferred_regeneration()
    log = (tmp_path / "gh.log").read_text(encoding="utf-8")
    assert "could not RUN" in capsys.readouterr().out
    assert "could not be regenerated from" not in log
    assert "auto-resolve/handed-off" not in log


_MERGED_SOURCE_CRASH = (
    'echo "Traceback (most recent call last):" >&2;'
    ' echo "  File \\"rules.py\\", line 9, in build" >&2;'
    " echo \"KeyError: 'step-id'\" >&2; exit 1"
)


def test_a_generator_that_RAISES_over_the_merged_sources_is_not_plumbing(
    step, tmp_path, monkeypatch, capsys
):
    """A generator that starts fine and then raises over the merged tree prints
    a traceback too. Reading that as provisioning blames the workflow for the
    branch, and leaves an unresolvable head unmarked to be retried forever."""
    _stub_pnpm(tmp_path, monkeypatch, _MERGED_SOURCE_CRASH)
    _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        step.verify_generated_artifacts()
    assert "could not RUN" not in capsys.readouterr().out


def test_a_module_THIS_TREE_provides_is_not_a_missing_dependency(
    step, tmp_path, monkeypatch, capsys
):
    """The merge can break a LOCAL import. That failure is the branch's, so the
    head takes the ordinary mark rather than a provisioning refusal."""
    (tmp_path / "work" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path / "work", "add", "helper.py")
    _stub_pnpm(
        tmp_path,
        monkeypatch,
        "echo \"ModuleNotFoundError: No module named 'helper'\" >&2; exit 1",
    )
    _stub_gh(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        step.verify_generated_artifacts()
    assert "could not RUN" not in capsys.readouterr().out


def _plumbing_refusal(tmp_path, monkeypatch, issue: str) -> str:
    """Drive one resolver-fault refusal and return what `gh` was asked to do."""
    verdicts = tmp_path / "verdicts.json"
    verdicts.write_text("{}", encoding="utf-8")
    step = _with_second_path(
        tmp_path,
        monkeypatch,
        MODIFY_DELETE_PATHS="b.md",
        MODIFY_DELETE_VERDICTS=str(verdicts),
        HEAD_SHA=_HEAD_SHA,
    )
    monkeypatch.setenv("AUTO_RESOLVE_PLUMBING_ISSUE", issue)
    with pytest.raises(SystemExit):
        step.stage_modify_delete()
    return (tmp_path / "gh.log").read_text(encoding="utf-8")


def test_a_plumbing_refusal_is_repeated_on_the_issue_the_caller_named(
    tmp_path, monkeypatch
):
    """Its only other surface is the sticky PR comment, which the next run
    overwrites and which whoever owns the BRANCH reads — not whoever owns the
    pins, the grants or the tooling that actually failed."""
    log = _plumbing_refusal(tmp_path, monkeypatch, "4242")
    assert "issue comment 4242" in log
    assert "auto-resolve-plumbing" in log


def test_a_caller_that_named_no_issue_gets_only_the_sticky_comment(
    tmp_path, monkeypatch
):
    """Nothing is posted uninvited: a consumer that wants no issue traffic keeps
    today's behaviour exactly."""
    log = _plumbing_refusal(tmp_path, monkeypatch, "")
    assert "issue comment" not in log
    assert "not even a decline" in log
