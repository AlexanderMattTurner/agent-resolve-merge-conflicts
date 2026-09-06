"""`setup-command` prepares the caller's checkout before the model runs.

A repository may need its checkout repaired before an agent can start in it at
all: `.dotfiles` tracks `.claude/hooks` as a symlink into an ignored directory,
so the link dangles in every CI checkout and Claude Code exits on the `statx`
before it reads a prompt — on every credential rung, which reads as a dead token
rather than a broken tree.

The command is a script the PR HEAD's own tree supplies, and the resolve job
holds every model credential, so it carries the same fork gate
`pre-pass-command` does. These cases EVALUATE the shipped conditions against
synthetic payloads rather than grepping them for a guard string: a guard present
but wired into the wrong arm still reds.

# covers: .github/workflows/auto-resolve.yaml
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from tests._gha_if import evaluate
from tests._helpers import REPO_ROOT

REUSABLE = REPO_ROOT / ".github" / "workflows" / "auto-resolve.yaml"

STEP = "Prepare the merged tree for the model"
PREPARE = "Merge base, deterministic pre-pass, and protected-path flag"
LADDER = "Resolve the remaining source conflicts with Claude"
BUNDLE = "Verify, self-review, and bundle the merge for the land job"
RELEASE = "Release this head's attempt mark (this run died before it spent)"

REPO = "owner/repo"
FORK = "outsider/repo"

# The same condition WITHOUT the fork gate. The fork case below is decided
# against it too, so a payload the shipped condition refuses is one this
# spelling admits — which is what proves the gate, and not some unrelated
# clause, is doing the refusing.
UNGUARDED = (
    "inputs.setup-command != '' && (steps.prepare.outputs.needs_llm == 'true' "
    "|| steps.prepare.outputs.needs_commit == 'true')"
)


def _steps() -> list[dict]:
    doc = yaml.safe_load(REUSABLE.read_text(encoding="utf-8"))
    return doc["jobs"]["resolve"]["steps"]


def _step(name: str) -> dict:
    found = {s["name"]: s for s in _steps() if "name" in s}
    assert name in found, f"{name} is gone from the resolve job"
    return found[name]


def _order(name: str) -> int:
    names = [s.get("name") for s in _steps()]
    return names.index(name)


def _decide(expression: str, context: dict, *, failed: bool) -> bool:
    """A condition that names a job-status function, decided for one job state.

    The evaluator models context paths, not the runner's own status functions,
    so they are pinned here: FAILED is the arm every case below is about, and
    no case is about a cancelled job.
    """
    resolved = expression.replace("cancelled()", "false")
    resolved = resolved.replace("always()", "true")
    resolved = resolved.replace("failure()", "true" if failed else "false")
    return evaluate(resolved, context)


def _context(command: str, head_repo: str, needs_llm: str, needs_commit: str) -> dict:
    return {
        "github": {"repository": REPO},
        "inputs": {"setup-command": command},
        "steps": {
            "prepare": {
                "outputs": {"needs_llm": needs_llm, "needs_commit": needs_commit}
            },
            "selected": {"outputs": {"head_repo": head_repo}},
        },
    }


@pytest.mark.parametrize(
    ("command", "head_repo", "needs_llm", "needs_commit", "runs", "why"),
    [
        ("bash prep.sh", REPO, "true", "", True, "a named command on a same-repo head"),
        ("bash prep.sh", REPO, "", "true", True, "the self-review path alone"),
        ("bash prep.sh", FORK, "true", "true", False, "a fork head executes nothing"),
        ("", REPO, "true", "true", False, "an empty command runs nothing"),
        ("bash prep.sh", REPO, "", "", False, "no model call to prepare for"),
    ],
)
def test_the_condition_decides_who_gets_prepared(
    command: str,
    head_repo: str,
    needs_llm: str,
    needs_commit: str,
    runs: bool,
    why: str,
) -> None:
    context = _context(command, head_repo, needs_llm, needs_commit)
    assert evaluate(str(_step(STEP)["if"]), context) is runs, why


def test_only_the_fork_gate_refuses_a_fork_head() -> None:
    """Non-vacuity for the case that matters. The un-gated spelling ADMITS the
    same fork payload the shipped condition refuses, so the refusal above is the
    gate's doing."""
    context = _context("bash prep.sh", FORK, "true", "true")
    assert evaluate(UNGUARDED, context) is True
    assert evaluate(str(_step(STEP)["if"]), context) is False


def test_the_step_sits_between_the_merge_and_the_model() -> None:
    """BOTH bounds. Below `prepare` the command would rewrite files `git merge`
    then refuses to overwrite; above the ladder it would repair a checkout the
    model has already met. The lower bound fails SILENTLY on its own — moved
    above `prepare`, `steps.prepare.outputs.needs_llm` reads as the empty string,
    the condition goes false, and the step simply never runs again."""
    assert _order(PREPARE) < _order(STEP) < _order(LADDER)


def test_a_failed_preparation_bundles_nothing_and_hands_the_attempt_back() -> None:
    """The mark buys ONE resolve per head. A caller's typo must not spend it: the
    bundle step stands down, which is what lets the release step below return the
    mark instead of suppressing every later scan for its whole TTL."""
    context = {
        "github": {"repository": REPO},
        "inputs": {"setup-command": "bash prep.sh"},
        "steps": {
            "prepare": {"outputs": {"needs_commit": "true", "no_op_head": ""}},
            "setup": {"outcome": "failure"},
            "mark": {"outputs": {"head_sha": "deadbee", "already_claimed": "false"}},
            "ladder": {"outcome": "skipped"},
            "bundle": {"outcome": "skipped"},
            "handoff": {"outcome": "skipped"},
        },
    }
    assert _decide(str(_step(BUNDLE)["if"]), context, failed=True) is False
    assert _decide(str(_step(RELEASE)["if"]), context, failed=True) is True


def test_a_prepared_run_still_bundles() -> None:
    """The other arm, so the clause above cannot be passing by refusing always."""
    context = {
        "github": {"repository": REPO},
        "inputs": {"setup-command": "bash prep.sh"},
        "steps": {
            "prepare": {"outputs": {"needs_commit": "true", "no_op_head": ""}},
            "setup": {"outcome": "success"},
        },
    }
    assert _decide(str(_step(BUNDLE)["if"]), context, failed=False) is True


@pytest.mark.parametrize(
    ("command", "code", "why"),
    [
        ("printf ok >'{marker}'", 0, "a command that succeeds"),
        ("false; printf ok >'{marker}'", 1, "a `;` chain whose FIRST half fails"),
        ("false && printf ok >'{marker}'", 1, "an `&&` chain"),
        ("mkdir -p '{marker}.d' && printf ok >'{marker}'", 0, "quoting survives"),
    ],
)
def test_the_run_line_carries_the_shell_posture(tmp_path, command, code, why) -> None:
    """The shipped `run:`, driven for real. The runner gives the OUTER shell
    `-e`, and an inner `bash -c` starts fresh, so without the flags only the last
    command's status escapes and a half-failed preparation reports success."""
    marker = tmp_path / "prepared"
    # A scratch repository, because the record scripts sample a git worktree and
    # this case is about the SHELL the command runs in, not about any tree.
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "-C", str(work), "init", "-q"], check=True)
    done = _run_the_step(work, tmp_path / "record.json", command.format(marker=marker))
    assert done.returncode == code, f"{why}: {done.stderr}"


def _conflicted_repo(
    work: Path,
    *,
    ours_deletes: bool = False,
    executable: bool = False,
    name: str = "pins.sh",
) -> None:
    """A checkout sitting mid-merge, with `pins.sh` conflicted on both sides.

    The shape the shield exists for: a shell file the setup command sources, and
    a merge that left `<<<<<<<` in it. `ours_deletes` makes it a modify/delete
    conflict instead, which has no stage 2; `executable` makes it a file a
    command runs rather than sources.
    """

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True)

    work.mkdir(parents=True, exist_ok=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    body = "#!/bin/sh\necho {pin}\n" if executable else "PIN={pin}\n"
    pins = work / name
    pins.write_text(body.format(pin="base"), encoding="utf-8")
    if executable:
        pins.chmod(0o755)
    run("add", name)
    run("commit", "-qm", "base")
    run("checkout", "-q", "-b", "other")
    pins.write_text(body.format(pin="theirs"), encoding="utf-8")
    run("commit", "-qam", "theirs")
    run("checkout", "-q", "main")
    if ours_deletes:
        run("rm", "-q", name)
        run("commit", "-qm", "ours deletes")
    else:
        pins.write_text(body.format(pin="ours"), encoding="utf-8")
        run("commit", "-qam", "ours")
    subprocess.run(
        ["git", "-C", str(work), "merge", "other"], check=False, capture_output=True
    )
    if not ours_deletes:
        assert "<<<<<<<" in pins.read_text(encoding="utf-8")


def _run_the_step(
    work: Path, record: Path, command: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-e", "-c", str(_step(STEP)["run"])],
        env={
            "PATH": "/usr/bin:/bin",
            "RESOLVER_DIR": str(REPO_ROOT / ".github" / "resolver"),
            "AUTO_RESOLVE_SETUP_RECORD": str(record),
            "AUTO_RESOLVE_SETUP_COMMAND": command,
        },
        cwd=work,
        capture_output=True,
        text=True,
    )


def test_a_command_that_sources_a_conflicted_file_still_runs(tmp_path) -> None:
    """The defect this shield fixes. Bash parses `<<<<<<<` as syntax, so the
    unshielded tree kills the step with `syntax error near unexpected token`
    before the model is ever called."""
    work = tmp_path / "work"
    _conflicted_repo(work)
    done = _run_the_step(
        work, tmp_path / "record.json", 'source ./pins.sh && printf %s "$PIN" >pin.txt'
    )
    assert done.returncode == 0, done.stderr
    assert (work / "pin.txt").read_text(encoding="utf-8") == "ours"


def test_the_markers_are_back_after_the_command(tmp_path) -> None:
    """The shield is a loan. A tree left holding one parent's content is a tree a
    later step could commit as a resolution nobody wrote."""
    work = tmp_path / "work"
    _conflicted_repo(work)
    done = _run_the_step(work, tmp_path / "record.json", "source ./pins.sh")
    assert done.returncode == 0, done.stderr
    body = (work / "pins.sh").read_text(encoding="utf-8")
    assert "<<<<<<<" in body and "PIN=ours" in body and "PIN=theirs" in body


def test_a_failed_command_leaves_the_markers_in_place(tmp_path) -> None:
    """The `finally` arm. A command that dies must not strand the shield."""
    work = tmp_path / "work"
    _conflicted_repo(work)
    done = _run_the_step(work, tmp_path / "record.json", "source ./pins.sh && false")
    assert done.returncode == 1, done.stderr
    assert "<<<<<<<" in (work / "pins.sh").read_text(encoding="utf-8")


def test_a_modify_delete_conflict_is_left_exactly_as_the_merge_left_it(
    tmp_path,
) -> None:
    """Only a both-sides conflict carries markers. A modify/delete already holds
    one parent's content, so shielding it would hand the command a version the
    merge did not leave there."""
    work = tmp_path / "work"
    _conflicted_repo(work, ours_deletes=True)
    before = (work / "pins.sh").read_text(encoding="utf-8")
    done = _run_the_step(
        work, tmp_path / "record.json", 'source ./pins.sh && printf %s "$PIN" >pin.txt'
    )
    assert done.returncode == 0, done.stderr
    assert (work / "pin.txt").read_text(encoding="utf-8") == "theirs"
    assert (work / "pins.sh").read_text(encoding="utf-8") == before


def test_a_non_ascii_path_is_shielded_too(tmp_path) -> None:
    """`git ls-files -u` QUOTES such a path — `"caf\\303\\251.sh"` — and
    `cat-file blob :2:"caf\\303\\251.sh"` then exits 128, so the path is read as
    unshieldable and the original bash-syntax death comes back."""
    work = tmp_path / "work"
    _conflicted_repo(work, name="café.sh")
    done = _run_the_step(
        work, tmp_path / "record.json", 'source ./café.sh && printf %s "$PIN" >pin.txt'
    )
    assert done.returncode == 0, done.stderr
    assert (work / "pin.txt").read_text(encoding="utf-8") == "ours"
    assert "<<<<<<<" in (work / "café.sh").read_text(encoding="utf-8")


def test_a_conflicted_executable_is_shielded_as_one(tmp_path) -> None:
    """The other way a command reads a conflicted file is by RUNNING it, which
    needs the mode back as well as the content."""
    work = tmp_path / "work"
    _conflicted_repo(work, executable=True)
    done = _run_the_step(work, tmp_path / "record.json", "./pins.sh >pin.txt")
    assert done.returncode == 0, done.stderr
    assert (work / "pin.txt").read_text(encoding="utf-8") == "ours\n"


def test_the_input_is_declared_and_defaults_to_nothing() -> None:
    doc = yaml.safe_load(REUSABLE.read_text(encoding="utf-8"))
    declared = doc[True]["workflow_call"]["inputs"]["setup-command"]
    assert declared["required"] is False
    assert declared["default"] == "", (
        "an unset setup-command must run nothing, so every existing caller is "
        "unaffected"
    )
