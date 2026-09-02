"""Did a caller's command REPORT, or did it die?

PROBLEM CLASS — a tool's non-zero exit read as a verdict about the merged tree
when the tool never reached a verdict at all. The resolver runs commands the
CALLING repository names — a pre-pass that re-derives generated files, a check
over the merged tree — and a non-zero status from one is ambiguous: it means
"I looked and the tree is wrong", or it means "I could not run".

Reading the second as the first blames the branch for the workflow's own
provisioning. On agent-glovebox #5521 a generated-file rule did not pin
`pyyaml`, the verifier died importing `yaml`, and the run told a human the
resolution held bytes no build produces — over a file the conflict never
touched, before any merge decision was made.
"""

import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    fail,
    report_block,
)

# The shell's floor for "the command never ran": 126 (found, not executable),
# 127 (not found) and every 128+signal, which includes an OOM kill. Below it the
# command RAN and reported, so its status is a verdict.
NEVER_RAN = 126

# An UNHANDLED interpreter death, in the two languages a caller's command runs
# in. A tool that looked and found a fault reports it; one that dies unwinding
# its own imports reached nothing to report. Each of these lines is written by
# the interpreter itself, never by a tool describing what it found.
_CRASHED = re.compile(
    r"Traceback \(most recent call last\):|Cannot find module|ERR_MODULE_NOT_FOUND"
)


def never_produced_a_verdict(done: subprocess.CompletedProcess) -> bool:
    """Whether DONE's non-zero status says the command could not run, rather
    than what it found.

    Two signals, because the exit status alone misses the case that bites: a
    Python or Node process that starts fine and then dies importing a module
    exits 1, which is indistinguishable from an ordinary "I found a fault".
    Its output is not — the interpreter prints its own crash, and no tool
    reporting a finding does.
    """
    if done.returncode == 0:
        return False
    if done.returncode >= NEVER_RAN:
        return True
    return bool(_CRASHED.search((done.stdout or "") + (done.stderr or "")))


def refuse_a_command_that_never_ran(
    done: subprocess.CompletedProcess, argv: list[str]
) -> None:
    """INVARIANT — this is what stops a caller command's CRASH being reported as
    a verdict about the merge. A non-zero status from one reads as "the merged
    bytes are wrong", which the branch owns; a command that died provisioning
    its own interpreter judged nothing, and the fix lands in the workflow.

    No mark on the head, so a re-run after the provisioning fix answers again
    instead of stranding the branch behind an attempt that was never made."""
    if not never_produced_a_verdict(done):
        return
    named = shlex.join(argv)
    fail(
        f"the caller's pre-pass could not RUN (`{named}` exited "
        f"{done.returncode}), so nothing re-derived the generated files",
        f"the generated file(s) could NOT be checked: `{named}` exited "
        f"{done.returncode} without reporting — a missing tool, an unpinned "
        "dependency of its own, or a signal that killed it. That is a defect "
        "in this workflow's provisioning, **not** a problem with the "
        "resolution or with your branch.",
        resolver_fault=True,
        report=report_block(done.stdout + done.stderr),
    )
