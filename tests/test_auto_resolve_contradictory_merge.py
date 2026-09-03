"""Tests for the CONTRADICTORY-MERGE checks (agent-glovebox #5641: two merges
whose every line traced to a parent and whose trees could not work).

Each case hands the two analysis functions the three sides of a real merge as
text, which is what the bundle step reads them out of git as."""

from tests._resolver_helpers import load_script

contradictory_merge = load_script(
    ".github/resolver/auto-resolve/_contradictory_merge.py"
)
orphaned_added_names = contradictory_merge.orphaned_added_names
resurrected_lines = contradictory_merge.resurrected_lines

# The Docker Hub cooldown wait from instance 1, as the merge base held it.
_BASE = """import time


def wait(hold):
    time.sleep(hold.seconds_left)
"""
# The branch's side: a module-local alias, and the one wait pointed at it.
_ALIASED = """import time

_sleep = time.sleep


def wait(hold):
    _sleep(hold.seconds_left)
"""


def test_an_alias_the_merge_left_with_no_reader_is_reported():
    # The merge kept the branch's alias and the base branch's plain call, so
    # `_sleep` has zero call sites and every fixture patching it patches a name
    # nothing reads.
    merged = """import time

_sleep = time.sleep


def wait(hold):
    time.sleep(hold.seconds_left)
"""
    assert orphaned_added_names(_BASE, [_ALIASED, _BASE], merged) == ["_sleep"]


def test_an_alias_the_merge_still_calls_is_not_reported():
    # The correct resolution of the same conflict. Nothing here is a finding.
    assert orphaned_added_names(_BASE, [_ALIASED, _BASE], _ALIASED) == []


def test_a_name_the_parent_itself_never_read_is_not_reported():
    # A helper one side added for OTHER modules to import. Its own module never
    # read it, so a merged file that does not read it either lost nothing — and
    # reporting it would refuse every added export.
    added = _BASE + "\n\ndef helper():\n    return 1\n"
    assert orphaned_added_names(_BASE, [added, _BASE], added) == []


# The always-on deny assertions from instance 2, as the merge base held them.
_DENY_BASE = """def test_deny(deny):
    for path in ("//etc/claude-code/**", "//run/monitor-secret/**"):
        assert f"Read({path})" in deny, deny
        assert f"Edit({path})" in deny, deny
"""
_READ_ASSERTION = 'assert f"Read({path})" in deny, deny'
# The base branch's side: the read deny assertion removed.
_DENY_MAIN = """def test_deny(deny):
    for path in ("//etc/claude-code/**", "//run/monitor-secret/**"):
        assert f"Edit({path})" in deny, deny
"""
# The branch's side: the same removal, plus the negation it replaced it with.
_DENY_BRANCH = """def test_deny(deny):
    for path in ("//etc/claude-code/**", "//run/monitor-secret/**"):
        assert f"Edit({path})" in deny, deny
        assert f"Read({path})" not in deny, deny
"""


def test_a_line_every_parent_deleted_and_the_merge_restored_is_reported():
    # The merged loop body carries the assertion and its negation, so no deny
    # list satisfies it. Both parents' own commits had removed the assertion.
    merged = """def test_deny(deny):
    for path in ("//etc/claude-code/**", "//run/monitor-secret/**"):
        assert f"Read({path})" in deny, deny
        assert f"Edit({path})" in deny, deny
        assert f"Read({path})" not in deny, deny
"""
    assert resurrected_lines(_DENY_BASE, [_DENY_BRANCH, _DENY_MAIN], merged) == [
        _READ_ASSERTION
    ]


def test_a_line_one_parent_still_carries_is_not_reported():
    # Only the branch dropped the assertion. The base branch still ships it, so
    # a merge that keeps it traces to that parent and is an ordinary resolution.
    assert resurrected_lines(_DENY_BASE, [_DENY_BRANCH, _DENY_BASE], _DENY_BASE) == []


def test_a_line_the_merge_base_repeated_is_not_reported():
    # The same assertion in two loops. Both parents dropped both copies and the
    # merge holds one, but nothing says which copy that is or that a merge put
    # it there, so a repeated line is out of this check's reach.
    base = _DENY_BASE + "\n" + _DENY_BASE.replace("test_deny", "test_deny_again")
    merged = _DENY_MAIN + "\n" + _DENY_BASE.replace("test_deny", "test_deny_again")
    stripped = _DENY_MAIN + "\n" + _DENY_MAIN.replace("test_deny", "test_deny_again")
    assert _READ_ASSERTION in merged
    assert resurrected_lines(base, [stripped, stripped], merged) == []
