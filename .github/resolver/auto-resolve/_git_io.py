"""How the auto-resolve BUNDLE step runs git, and how it undoes a merge.

Split out of bundle.py so the step and the refusal path beside it both reach git
through one definition rather than carrying their own subprocess wrapper.

PROBLEM CLASS — a git call here names its repository, and never inherits one.
`abort_merge_if_in_progress` runs `git merge --abort`, and the in-process suite
drives this module inside a developer's own checkout. A call that took the
process working directory would abort THAT tree's merge: the developer's staged
resolution is discarded, HEAD stays put, and git prints nothing a session would
read as damage. So `bind_repo` is required before the first call, `_argv` puts
`-C <repo>` on every invocation, and an unbound call raises instead of guessing.
ci-truth-serum's `check_cwd_scoped_git` holds the same rule over every git
argv this tree builds in Python.
"""

import shlex
import subprocess
import sys
from pathlib import Path

_REPO: Path | None = None

# git's own exit codes for `merge-file`: 0 clean, 1..127 that many conflicts,
# anything above an error, and a negative value a signal.
MERGE_FILE_MAX_CONFLICTS = 127
# What `git check-attr merge` may answer for a path a caller may line-merge
# itself. Anything else — `-merge` (unset), or a named driver — is a merge
# policy the repository configured, and `git merge-file` dispatches on neither.
PLAIN_MERGE_ATTRS = frozenset({"unspecified", "set"})


def merge_file_failed(returncode: int) -> bool:
    """RETURNCODE says `git merge-file` could not merge at all.

    A conflict is not a failure: git reports it as the number of conflicts, and
    the answer still carries markers the caller can use.
    """
    return returncode < 0 or returncode > MERGE_FILE_MAX_CONFLICTS


def bind_repo(path: str | Path) -> Path:
    """Name the repository every call below acts on, and return its root.

    Resolved to the worktree root through git itself, so a caller may hand in
    any directory inside the checkout. Raises when `path` is not in one.
    """
    global _REPO
    done = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        sys.stderr.write(done.stderr)
        raise SystemExit(done.returncode)
    _REPO = Path(done.stdout.strip())
    return _REPO


def _reset_process_state() -> None:
    """Forget the bound repository, so a run cannot inherit the last one's.

    The binding is the whole safety property here, and a long-lived worker
    importing this module once would otherwise carry one run's checkout into the
    next. Unbound is the safe state: the next call refuses instead of guessing.
    """
    global _REPO
    _REPO = None


def bound_repo() -> Path:
    """The repository `bind_repo` named. Raises when nothing named one."""
    if _REPO is None:
        raise RuntimeError(
            "_git_io is unbound: call bind_repo(<checkout>) before any git call, "
            "so a destructive command cannot reach whatever tree the process "
            "happens to be sitting in."
        )
    return _REPO


def _argv(args: tuple[str, ...]) -> list[str]:
    return ["git", "-C", str(bound_repo()), *args]


class GitCallFailed(Exception):
    """A git call exited non-zero, carrying the command and what it printed.

    An ordinary EXCEPTION and never `SystemExit`, which is what makes it
    catchable: bundle.py's top-level guard turns this into a refusal the pull
    request can read, and a `SystemExit` passes that guard untouched and ends
    the step with a bare exit code no comment on the pull request explains.
    """

    def __init__(self, argv: list[str], returncode: int, output: str) -> None:
        self.command = shlex.join(argv)
        self.returncode = returncode
        self.output = output
        super().__init__(f"`{self.command}` exited {returncode}")


def git(*args: str, check: bool = True) -> str:
    argv = _argv(args)
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    if check and done.returncode != 0:
        sys.stderr.write(done.stderr)
        raise GitCallFailed(argv, done.returncode, done.stdout + done.stderr)
    return done.stdout


def git_bytes(*args: str) -> bytes | None:
    """One git call's stdout as raw BYTES, or None when the call failed.

    For a caller that reads a BLOB — `git show :2:<path>` — and must keep the
    line endings git recorded. Text mode decodes through universal newlines, so
    a CRLF file arrives with every line ending already rewritten to a bare LF.
    """
    done = subprocess.run(_argv(args), capture_output=True, check=False)
    return None if done.returncode != 0 else done.stdout


def git_result(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    """One git call's whole result — status, stdout and stderr.

    For the caller that must REPORT why a command failed and carry on, rather
    than exit on it: `git` above exits, and `git_status` throws both streams
    away. `stdin` feeds the plumbing commands that read one (`update-index
    --index-info`)."""
    return subprocess.run(
        _argv(args), capture_output=True, text=True, check=False, input=stdin
    )


def git_status(*args: str) -> int:
    """Run git for its exit status alone, discarding both streams."""
    return subprocess.run(
        _argv(args), capture_output=True, text=True, check=False
    ).returncode


def git_lines(*args: str) -> list[str]:
    return [line for line in git(*args).splitlines() if line]


def abort_merge_if_in_progress() -> None:
    """Undo the conflicted merge when one is still open.

    `git merge --abort` is valid ONLY while MERGE_HEAD exists — on prepare's
    clean-merge path, and after the bundle step's own commit, it dies with
    "fatal: There is no merge to abort", a red herring in the log for a cleanup
    with nothing to do. The merge then exists only in this ephemeral runner
    checkout and was never bundled, so leaving it in place IS the correct
    restore; say so.
    """
    if git_status("rev-parse", "-q", "--verify", "MERGE_HEAD") != 0:
        print(
            "no merge in progress — the local merge commit was never bundled, "
            "so there is nothing to abort."
        )
        return
    if git_status("merge", "--abort") != 0:
        print(
            "::warning::git merge --abort failed; the conflicted tree stays as-is "
            "(this checkout is discarded).",
            file=sys.stderr,
        )
