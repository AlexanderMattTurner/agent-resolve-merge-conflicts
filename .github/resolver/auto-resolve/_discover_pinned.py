#!/usr/bin/env python3
"""Judging a PR against a pinned base-side commit (the `base-sha` input),
instead of the base branch's own tip.

An underscore filename so ``discover.py`` can ``import`` it outright: the
hyphenated scripts beside it load each other through ``importlib``, and a type
reached that way is a runtime attribute pyright cannot resolve in an
annotation.
"""

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from _discover_types import PullRequest

# The real-merge probe a pinned base side is judged by, and the one builder of a
# github.com git auth header, which the probe's clone of the base repository runs
# under so a private caller's repository answers too.
_MERGE_PROBE = Path(__file__).resolve().parent.parent / "merge-conflict-probe.py"
_GIT_AUTH_LIB = Path(__file__).resolve().parent.parent / "lib" / "git-auth.bash"
_WITH_GIT_AUTH = 'source "$1" && git_auth_header "$GH_TOKEN" && shift && exec "$@"'
# A blobless clone of the base repository plus one merge. Bounded, because this
# runs in the resolve job's first step, ahead of every stage its budget covers.
_MERGE_PROBE_SECONDS = 300


@dataclass(frozen=True)
class PinnedBaseRefused:
    """The caller pinned a base-side commit, and the real merge of it into this
    head did not conflict or could not run.

    VERDICT is the probe's word (:func:`probe_pinned_base`), or None when the
    probe itself failed."""

    pr: PullRequest
    verdict: str | None


def probe_pinned_base(
    executable: str, clone_url: str, pr_number: int, base_ref: str, base_sha: str
) -> str | None:
    """What a REAL merge of BASE_SHA into PR_NUMBER's head says: CONFLICTING,
    MERGEABLE, BASE_SHA_MALFORMED or BASE_SHA_UNREACHABLE. None when that merge
    could not run at all.

    merge-conflict-probe.py owns the merge and both refusals, so the labeler
    and discover.py's scan judge a conflict the same way. EXECUTABLE is the
    interpreter that runs the probe (``discover.py``'s own ``sys.executable``,
    so a test double can substitute a different one)."""
    try:
        done = subprocess.run(
            [
                "bash",
                "-c",
                _WITH_GIT_AUTH,
                "_",
                str(_GIT_AUTH_LIB),
                executable,
                str(_MERGE_PROBE),
                "--clone-url",
                clone_url,
            ],
            input=f"{pr_number}\t{base_ref}\t{base_sha}\n",
            capture_output=True,
            text=True,
            check=False,
            timeout=_MERGE_PROBE_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(f"::warning::the merge probe for PR #{pr_number} timed out.")
        return None
    print(done.stderr, end="", file=sys.stderr)
    if done.returncode != 0:
        return None
    verdicts = dict(
        line.split("\t", 1) for line in done.stdout.splitlines() if "\t" in line
    )
    return verdicts.get(str(pr_number))
