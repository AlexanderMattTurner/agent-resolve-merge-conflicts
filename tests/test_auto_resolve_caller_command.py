"""One command the CALLING repository named, and the two questions about it.

`_caller_command.py` answers how a command line becomes an argv, and whether a
non-zero status is a verdict about the merged tree or a report that the command
never reached one. Both answers are read by shell steps and by Python steps, so
the cases here drive the CLI protocol as a shell consumes it and the predicates
as their callers call them.
"""

# covers: .github/resolver/auto-resolve/_caller_command.py
# covers: .github/resolver/auto-resolve/_hook_gate.py
# covers: .github/resolver/auto-resolve/_tool_verdict.py
# covers: .github/resolver/auto-resolve/_post_merge_check.py

import os
import subprocess
import sys

import pytest

from tests._helpers import init_test_repo
from tests._resolver_helpers import REPO_ROOT, git_env, load_script

_AUTO_RESOLVE = REPO_ROOT / ".github" / "resolver" / "auto-resolve"
_CALLER_COMMAND = _AUTO_RESOLVE / "_caller_command.py"

sys.path.insert(0, str(_AUTO_RESOLVE))

caller_command = load_script(".github/resolver/auto-resolve/_caller_command.py")
hook_gate = load_script(".github/resolver/auto-resolve/_hook_gate.py")
tool_verdict = load_script(".github/resolver/auto-resolve/_tool_verdict.py")


def _done(returncode: int, output: str = "") -> subprocess.CompletedProcess:
    """A finished command, as the callers of `never_produced_a_verdict` hold one."""
    return subprocess.CompletedProcess([], returncode, stdout=output, stderr="")


# --- which log line names an environment fault --------------------------------


@pytest.mark.parametrize(
    "log",
    [
        "> pnpm -s resolve-generated\nsh: 1: pnpm: not found\npnpm: command not found\n",
        "Error: Cannot find module ../lib/gen\n    at Module._resolveFilename\n",
        "Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'zx' imported from x\n",
        '  File "gen.py", line 1\nModuleNotFoundError: No module named yaml\n',
        "Traceback:\nModuleNotFoundError: No module named 'yaml'\n",
    ],
    ids=[
        "command_not_found",
        "cannot_find_module_unquoted",
        "err_module_not_found",
        "module_not_found_unquoted",
        "module_not_found_quoted",
    ],
)
def test_every_wording_an_absent_tool_or_module_produces_is_recognised(log):
    """A pre-pass that could not START re-derived nothing, so prepare.sh must tell
    that apart from a generator that ran and died on conflicted sources. Each
    wording here is one alternative of that question, and a wording this misses
    reads as a fault in the merged tree — it blames the branch for the runner.

    The whole LINE comes back, not the matched fragment: prepare.sh quotes it to
    a human, and the words around the match are what say which step printed it.
    """
    line = caller_command.missing_tool_line(log)
    assert line is not None
    assert line in log.splitlines()


def test_a_generator_that_ran_and_died_names_no_environment_fault():
    """The refusing direction, which is what makes the case above mean anything: a
    traceback over sources git left conflicted is the TREE's fault, and FINALIZE
    re-derives past it rather than exiting 78 and handing the branch to a human.
    """
    crash = (
        'Traceback (most recent call last):\n  File "gen.py", line 8\n'
        "SyntaxError: invalid syntax\n"
    )
    assert caller_command.missing_tool_line(crash) is None


def test_the_earliest_faulting_line_wins_whichever_wording_it_uses():
    """One line is quoted back as what the run saw, and a reader takes it for the
    fault that started every later one. So the answer is positional — the first
    line that matches — and never a preference between the two wordings.
    """
    shell_first = "a: pnpm: command not found\nb: No module named 'yaml'\n"
    module_first = "a: No module named 'yaml'\nb: pnpm: command not found\n"
    assert caller_command.missing_tool_line(shell_first) == "a: pnpm: command not found"
    assert caller_command.missing_tool_line(module_first) == "a: No module named 'yaml'"


# --- which status says the command never reached a verdict --------------------


@pytest.mark.parametrize(
    ("returncode", "crashed"),
    [
        (1, False),
        (2, False),
        (78, False),
        (125, False),
        (126, True),
        (127, True),
        (137, True),
        (-9, True),
    ],
    ids=[
        "found_a_fault",
        "found_a_fault_2",
        "misconfigured_tree",
        "found_a_fault_125",
        "not_executable",
        "not_found",
        "shell_saw_a_kill",
        "python_saw_a_kill",
    ],
)
def test_only_a_status_no_program_chooses_reads_as_a_command_that_never_ran(
    returncode, crashed
):
    """The caller names an arbitrary program, so the acceptance set may hold only
    statuses that program could not have picked for itself: the shell's two
    could-not-execute codes, and the two spellings of a signal kill (a shell
    reports 128+signal, `subprocess` reports the negative).

    78 is the one that bites. A checker exits it for a configuration file the
    merge broke — a real verdict about the merged tree — and taking it for a
    crash discards that verdict and blames this workflow's provisioning instead.
    """
    assert tool_verdict.never_produced_a_verdict(_done(returncode)) is crashed


def test_one_hook_that_rejected_the_content_outweighs_hooks_that_could_not_start():
    """pre-commit reports every hook it ran, so a report can hold both kinds at
    once. A hook that rejected the content JUDGED the resolution, and the repair
    pass answers that rejection — calling the whole report a provisioning fault
    throws a real refusal away and tells a human to audit their runner instead.
    """
    mixed = (
        "shellcheck..Failed\n- hook id: shellcheck\n- exit code: 127\n\n"
        "ruff........Failed\n- hook id: ruff\n- exit code: 1\n\nE501 line too long\n"
    )
    all_dead = (
        "shellcheck..Failed\n- hook id: shellcheck\n- exit code: 127\n\n"
        "gitleaks....Failed\n- hook id: gitleaks\n- exit code: 78\n"
    )
    assert hook_gate.hook_could_not_run(mixed) is False
    assert hook_gate.hook_could_not_run(all_dead) is True


# --- the argv the shell steps read -------------------------------------------


def test_the_argv_protocol_survives_an_argument_holding_whitespace():
    """prepare.sh reads this output with `mapfile -d ''`, so the words must be
    NUL-terminated: a quoted argument holding a space would otherwise arrive as
    two words and name a program nothing on PATH runs. Driven through a real
    bash, because the reader is bash and the belief under test is about it.
    """
    command = "'my dir/gen.sh' --out 'a b'"
    read_back = subprocess.run(
        [
            "bash",
            "-c",
            'mapfile -d "" -t argv < <("$1" "$2" --argv "$3"); printf "%s\\n" "${#argv[@]}" "${argv[@]}"',
            "bash",
            sys.executable,
            str(_CALLER_COMMAND),
            command,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert read_back.stdout.splitlines() == ["3", "my dir/gen.sh", "--out", "a b"]


@pytest.mark.parametrize("mode", ["--program", "--argv"])
def test_a_command_whose_quoting_never_closes_refuses_before_the_merge(mode):
    """prepare.sh reads these two modes BEFORE `git merge`, and a shell reads an
    exit status. So the answer is a refusal naming the input, not a traceback and
    not a usable argv — refusing here costs no resolution at all.

    `mapfile` returns 0 whatever the process substitution did, so an answer that
    merely came back empty would leave the caller running nothing and calling it
    a re-derivation.
    """
    done = subprocess.run(
        [sys.executable, str(_CALLER_COMMAND), mode, "pnpm 'resolve-generated"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 78
    assert done.stdout == ""
    assert "no closing quotation" in done.stderr
    assert "Traceback" not in done.stderr


def test_the_in_process_readers_take_the_unclosed_line_whole():
    """The two readers that ask PAST the point of no return — `_pre_pass` at
    import of the bundle step, `_post_merge_check` after every shard is resolved
    — cannot refuse with an exit status, and a raise there ends the step with a
    traceback. An EMPTY answer is worse: every reader takes that for "this caller
    declared no command" and skips the pre-pass or the check in silence.

    So the line survives as ONE word, which no runner can execute.
    """
    unclosed = "pnpm 'resolve-generated"
    assert caller_command.configured_argv(unclosed) == [unclosed]


def test_the_post_merge_check_refuses_in_words_when_its_command_will_not_split(
    tmp_path,
):
    """The other reader of that answer. `run` used its own `shlex.split`, so an
    unclosed quote raised `ValueError` out of a step the model had already been
    paid for, and the run reported a conflict it could not merge.

    Driven in a child interpreter because the refusal binds a repository and then
    exits the process.
    """
    init_test_repo(tmp_path)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=tmp_path,
        env=git_env(),
        check=True,
    )
    driver = (
        "import sys\n"
        f"sys.path.insert(0, {str(_AUTO_RESOLVE)!r})\n"
        "import _git_io\n"
        f"_git_io.bind_repo({str(tmp_path)!r})\n"
        "import _post_merge_check\n"
        "_post_merge_check.run(untrusted_head=False)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            **git_env(),
            "AUTO_RESOLVE_POST_MERGE_CHECK": "pnpm 'check",
        },
    )
    assert done.returncode != 0
    assert "ValueError" not in done.stderr
    assert "will not run on this runner" in done.stdout


# --- what the plumbing refusal tells a human to fix ---------------------------


def _refusal(monkeypatch, done: subprocess.CompletedProcess) -> tuple[str, str]:
    """The (error, comment) `refuse_a_command_that_never_ran` publishes for DONE."""
    said: list[tuple[str, str]] = []

    def _capture(error, comment, **_):
        said.append((error, comment))
        raise SystemExit(1)

    monkeypatch.setattr(tool_verdict, "fail", _capture)
    with pytest.raises(SystemExit):
        tool_verdict.refuse_a_command_that_never_ran(
            done, ["pnpm", "resolve-generated"]
        )
    return said[0]


def test_the_refusal_names_the_module_that_classified_the_failure(tmp_path):
    """The module name is the remedy, and a traceback under a fold is not read.

    agent-glovebox#5616 took four runs whose headline offered a three-way guess
    while `No module named 'dockerfile_parse'` sat in the report below it. That
    run exited 1, so the missing-module line is what classified it.
    """
    repo = tmp_path / "merged"
    init_test_repo(repo)
    sys.modules["_git_io"].bind_repo(str(repo))
    output = (
        '  File "/w/.github/scripts/_comment_scan.py", line 36, in <module>\n'
        "    from dockerfile_parse import DockerfileParser\n"
        "ModuleNotFoundError: No module named 'dockerfile_parse'\n"
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        error, comment = _refusal(monkeypatch, _done(1, output))
    assert "dockerfile_parse" in error, error
    assert "dockerfile_parse" in comment, comment
    # The guess the module name replaces must be gone, not printed beside it.
    assert "an unpinned dependency of its own" not in error, error


def test_a_kill_does_not_blame_a_module_the_run_merely_logged():
    """A signal classifies from the status alone, so a ModuleNotFoundError the
    pre-pass caught and recovered from is not the reason it died. Naming it would
    send a maintainer after a pin instead of the OOM that killed the run."""
    output = (
        "ModuleNotFoundError: No module named 'optional_extra'\n"
        "continuing without it\n"
        "Killed\n"
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        error, comment = _refusal(monkeypatch, _done(137, output))
    assert "optional_extra" not in error, error
    assert "a missing tool" in comment, comment
    assert "could not import" not in error, error
    assert "so nothing re-derived the generated files" in error, error
