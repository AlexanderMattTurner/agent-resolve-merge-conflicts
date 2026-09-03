"""The calling repository's pre-pass command — how it re-derives generated files.

Shared by the bundle step and its deferred-regeneration mixin, so both name the
same command the same way.
"""

import os
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    run_or_refuse,
)


def untrusted_head() -> bool:
    """Whether this run merged a head the resolve job may not execute — a fork.

    INVARIANT — the hook passes run the MERGED tree's own pre-commit hooks,
    which on a fork head are code the fork's author wrote. This refusal is what
    keeps a fork's code out of the job holding every model credential; the pull
    request's own required checks judge the merged bytes instead."""
    return os.environ.get("AUTO_RESOLVE_UNTRUSTED_HEAD") == "true"


# How the CALLING repository re-derives its generated files, from the workflow's
# `pre-pass-command` input, split the way a shell would. Empty is a caller with
# no generators, and never a guess at one: a wrong command reports "nothing to
# re-derive" for files that needed it. A FORK head empties it too — the command
# is a script that head's manifest defines, and this job holds every credential.
PRE_PASS = (
    [] if untrusted_head() else shlex.split(os.environ.get("AUTO_RESOLVE_PRE_PASS", ""))
)


def run_pre_pass(*args: str) -> subprocess.CompletedProcess:
    """The caller's pre-pass with ARGS, output captured."""
    return run_or_refuse(
        [*PRE_PASS, *args],
        label="pre-pass command",
        input_name="pre-pass-command",
        lost="re-derive the generated files",
    )
