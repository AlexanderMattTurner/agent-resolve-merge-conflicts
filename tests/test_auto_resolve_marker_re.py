"""Tests that `.auto_resolve.conflict_marker_re` means ONE thing.

The string has two kinds of consumer and they do not share a regex dialect:
`grep -E`/`git grep -E` in lib.sh, prepare.sh, land.sh, bundle.py and
_marker_verdict.py read it as POSIX ERE, and `_conflict_hunks.py` compiles it
with Python's `re`. POSIX ERE has no backslash escapes inside a bracket
expression, so a `\\t` written there is the letter `t` to grep and a tab to
Python — a single spelling that reads two different sets. Every case below runs
BOTH engines over the same line, so a spelling only one of them agrees with
fails rather than drifting."""

import json
import re
import subprocess

import pytest

from tests._resolver_helpers import REPO_ROOT

MARKER_RE = json.loads(
    (REPO_ROOT / ".github/resolver/lib/shared-names.json").read_text(encoding="utf-8")
)["auto_resolve"]["conflict_marker_re"]

# What git writes, and what it does not. Each is one LINE, with the marker's own
# trailing bytes: the label git appends, the tab a driver can leave, the CR of a
# CRLF file, and the bare marker whose only tail is end of line.
_LINES = [
    ("bare marker", "=======", True),
    ("labelled marker", "<<<<<<< HEAD", True),
    ("diff3 base line", "||||||| merged common ancestors", True),
    ("closing marker", ">>>>>>> theirs", True),
    ("marker then a tab", "=======\t", True),
    ("marker then the CR of a CRLF file", "=======\r", True),
    # The letters the backslash spellings decay to under grep. Each is ordinary
    # text, and a pattern that answers here calls prose a conflict.
    ("marker then the letter t", "=======t", False),
    ("marker then the letter r", "=======r", False),
    ("six equals", "======", False),
    ("marker then a word", "=======head", False),
]


def _grep_matches(line: str) -> bool:
    """Whether GNU `grep -E` matches LINE, the way every shell consumer asks."""
    done = subprocess.run(
        ["grep", "-qE", MARKER_RE],
        input=line + "\n",
        text=True,
        check=False,
    )
    assert done.returncode in (0, 1), done.returncode
    return done.returncode == 0


@pytest.mark.parametrize(
    ("name", "line", "expected"),
    [pytest.param(*case, id=case[0]) for case in _LINES],
)
def test_both_engines_read_the_shared_marker_pattern_the_same(
    name: str, line: str, expected: bool
) -> None:
    assert _grep_matches(line) is expected, f"grep -E disagrees on {name}"
    assert bool(re.match(MARKER_RE, line)) is expected, f"python re disagrees on {name}"
