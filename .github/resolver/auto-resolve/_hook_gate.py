"""The repo's pre-commit hooks, as the auto-resolve BUNDLE step sees them.

Three questions live here, and all three are about the hook gate rather than
about any resolution: which hooks this job must REFUSE to run, whether a hook
that failed did so because it could not START, and how long the model repair pass
that answers a hook rejection may take.
"""

import os
import re
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _caller_command import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    status_never_ran,
)
from _exit_codes import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    EXIT_MISCONFIGURED,
)
from _refusal import fail  # noqa: E402,I001  # pylint: disable=wrong-import-position

PRECOMMIT_CONFIG = Path(".pre-commit-config.yaml")

#: pre-commit opens one block per hook it RAN with this line, so the text after
#: each is that hook's own report and nothing else.
_HOOK_BLOCK_RE = re.compile(r"^- hook id: ", re.MULTILINE)
#: The status pre-commit prints for a hook that FAILED. It prints none for a hook
#: that passed, so a block holding one is a failing hook's block.
_EXIT_CODE_RE = re.compile(r"^- exit code: (?P<code>[0-9]+)$", re.MULTILINE)
#: pre-commit's own wording for a `language: system` entry whose executable is
#: absent from PATH. It names a tool nowhere else in the report.
_NO_EXECUTABLE_RE = re.compile(r"^Executable .+ not found$", re.MULTILINE)

# The repair pass's whole wall-clock budget, shared across the credential ladder.
# It matches ONE fan-out shard's bound, so the resolve job's timeout covers the
# repair with a single term however many rungs a dead credential costs.
_REPAIR_BUDGET_DEFAULT = 600


#: Read from the fan-out ladder's published deadline. Absent on a run whose
#: fan-out never happened — a deterministic-only resolve — where the repair
#: keeps its own bound and nothing is donated.
_FANOUT_DEADLINE_ENV = "AUTO_RESOLVE_FANOUT_DEADLINE_EPOCH"


def repair_budget_seconds(now: float | None = None) -> int:
    """The repair pass's wall clock: its own bound plus the fan-out's leftovers.

    PROBLEM CLASS — a stage that takes a fixed slice of a shared budget starves
    when an earlier stage under-spends. The job's timeout is sized as the SUM of
    the stages, so a fan-out that finishes early leaves time no one can reach:
    agent-glovebox PR #5009 resolved every conflict with 6 minutes of its fan-out
    window unspent, then died on this pass at exactly its 600-second cap.

    Donating only what the fan-out did NOT spend is what keeps the sum intact —
    the repair can never take time a later stage was promised.
    """
    configured = shard_timeout_seconds()
    raw = os.environ.get(_FANOUT_DEADLINE_ENV, "").strip()
    # The same ASCII-digit form shard_timeout_seconds() uses below, whose comment
    # explains why: str.isdigit() is True for Unicode digits int() then accepts.
    if not re.fullmatch(r"[0-9]+", raw):
        return configured
    left = int(raw) - (time.time() if now is None else now)
    return configured + max(0, int(left))


def shard_timeout_seconds() -> int:
    """The repair ladder's total budget, read from the same env var that bounds a
    fan-out shard so one workflow setting governs both.

    A value that is set but is not a positive whole number of seconds is a
    refusal, not a fallback — the same posture fanout.seconds_from_env
    takes on the same variable. This bound is what keeps the ladder inside the
    resolve job's own timeout, so silently substituting a larger default would
    discard the configured bound on exactly the path nobody is watching: the
    deterministic-only run, where the fan-out step never ran to reject it.
    """
    raw = os.environ.get("SHARD_TIMEOUT_SECONDS", "")
    if not raw:
        return _REPAIR_BUDGET_DEFAULT
    # Matched as ASCII digits, the same way fanout.positive_int validates this very
    # variable. str.isdigit() is True for Unicode digits such as "²" that int() then
    # REJECTS, so the bare check reached int() and died with a traceback instead of
    # the refusal this branch words.
    if not re.fullmatch(r"[0-9]+", raw) or int(raw) <= 0:
        fail(
            "SHARD_TIMEOUT_SECONDS must be a positive whole number of seconds, "
            f"got '{raw}'",
            "the resolver job's `SHARD_TIMEOUT_SECONDS` is not a positive whole "
            "number of seconds, so the hook-repair pass has no budget it can "
            "trust.",
            resolver_fault=True,
        )
    return int(raw)


def _a_hook_that_could_not_start(block: str) -> bool:
    """Whether ONE hook's block in pre-commit's report shows a hook that failed to
    EXECUTE, rather than one that judged the content and rejected it.

    Two signals, and neither names a tool: pre-commit's own message for a
    `language: system` entry whose executable is absent from PATH, and an exit
    status that is either EXIT_MISCONFIGURED — a caller's hook wrapper returns
    that one for "the tool this gate drives is not provisioned here" — or one
    `_caller_command.status_never_ran` reads as a command that never ran.
    """
    if _NO_EXECUTABLE_RE.search(block):
        return True
    codes = [int(code) for code in _EXIT_CODE_RE.findall(block)]
    return bool(codes) and all(
        code == EXIT_MISCONFIGURED or status_never_ran(code) for code in codes
    )


def hook_could_not_run(report: str) -> bool:
    """Did EVERY hook that failed in pre-commit's REPORT fail because it could not
    START, so this JOB is under-provisioned and nothing judged the resolution?

    INVARIANT — one hook that REJECTED the content makes the whole report a
    verdict, however many others could not start: the repair pass answers that
    rejection, and calling the report a provisioning fault throws a real refusal
    away. The question is asked per hook block for exactly that reason, never
    over the whole text at once.

    A misclassification in either direction is a wording error, never a safety
    hole: both arms of the caller abort without bundling, so an environment fault
    this misses degrades to a content-blaming abort, never to an unlinted bundle.
    """
    blocks = _HOOK_BLOCK_RE.split(report)
    failing = [
        block
        for block in blocks
        if _EXIT_CODE_RE.search(block) or _NO_EXECUTABLE_RE.search(block)
    ]
    return bool(failing) and all(_a_hook_that_could_not_start(b) for b in failing)


#: What an entry runs that resolves the CHECKED-OUT project's environment, and so
#: installs whatever that head's lockfile names.
_PROJECT_ENV = "uv run"
#: A wrapper read below belongs to the calling repository, so nothing here bounds
#: its size. Past every hook wrapper and short of a file worth streaming.
_WRAPPER_READ_LIMIT = 1 << 20


def _reaches_the_project_env(entry: str, root: Path) -> bool:
    """Whether this hook entry runs `uv run`, itself or through a WRAPPER it names.

    Reading the entry alone takes `bash scripts/lint.sh` for a hook that needs
    nothing, and that wrapper is free to run `uv run --frozen` in its own body —
    the install this refusal exists to prevent, reached through one more file.
    agent-glovebox's actionlint hook was exactly that shape.

    Only a path INSIDE the calling repository is read, and only one level deep: a
    wrapper that reaches `uv run` through a second script it calls is a blind
    spot, and a hook that runs one is a hook this job still runs.
    """
    if _PROJECT_ENV in entry:
        return True
    try:
        tokens = shlex.split(entry)
    except ValueError:
        # A pygrep hook's entry is a REGEX rather than a command line, so its
        # quoting need not close. Such a hook spawns no process at all.
        return False
    for token in tokens:
        # A wrapper is named by a relative path. A bare word is the interpreter or
        # a flag, and an absolute one is not this repository's file.
        if "/" not in token or token.startswith(("-", "/")) or ".." in token:
            continue
        try:
            with (root / token).open(encoding="utf-8", errors="replace") as wrapper:
                body = wrapper.read(_WRAPPER_READ_LIMIT)
        except OSError:
            continue
        if _runs_it(body):
            return True
    return False


def _runs_it(body: str) -> bool:
    """Whether this SCRIPT runs `uv run`, read past its own comments.

    A raw substring test over a file reads a COMMENT as a call, and the wrapper that
    explains why it does not use `uv run` is the case that bites — agent-glovebox's
    `scripts/actionlint-run.sh` says exactly that. `shlex` drops a comment and keeps a
    quoted command body, so `bash -c "uv run x"` still counts.
    """
    try:
        return _PROJECT_ENV in " ".join(shlex.split(body, comments=True))
    except ValueError:
        # Quoting this lexer cannot close. Fail closed on the raw text: a hook this
        # job skips is one the pull request's own checks still run.
        return _PROJECT_ENV in body


def hooks_needing_the_project_env(config: Path = PRECOMMIT_CONFIG) -> list[str]:
    """The ids of every hook whose entry runs `uv run`, sorted.

    `uv run` resolves the project environment from the workspace, and the
    workspace in this job IS the pull request's head. Running one would let the
    pull request choose what this job installs and then executes, down to the
    ``git+https://`` URL its dev extra pins a package to — the boundary
    install-hook-tools.sh holds by taking every pin from the trusted base ref.
    This refusal to run them is what keeps that boundary, and the pull request's
    own CI, which runs the full suite against the pushed merge, is the
    enforcement point instead.

    Derived from the config rather than listed beside it in the workflow,
    because a hand-copied list drifts in the direction that matters: a new
    `uv run` hook would run here, and the entry that reveals it is the one the
    copy does not have.
    """
    # No config means `pre-commit run` finds none either and runs NO hook at all,
    # so there is nothing to refuse — this is an empty set, not a bypassed one.
    if not config.is_file():
        return []
    # Imported HERE, not at module scope: a calling repository with no pre-commit
    # config never reaches this line, and a top-level import would make PyYAML a
    # hard requirement of every `bundle.py` run in every caller. On the path that
    # does reach it, install-hook-tools.sh has already installed pyyaml into this
    # interpreter from the trusted base ref's pin and asserted the import.
    import yaml  # pylint: disable=import-outside-toplevel

    doc = yaml.safe_load(config.read_text(encoding="utf-8"))
    return sorted(
        hook["id"]
        for repo in doc["repos"]
        for hook in repo["hooks"]
        if _reaches_the_project_env(hook.get("entry", ""), config.parent)
    )
