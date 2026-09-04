"""The analysis behind `_orphaned_binding`, driven on the shape that produced it.

agent-glovebox #5568 merged one parent's `_sleep = time.sleep` alias with the
other parent's `time.sleep(...)` call. Every line traced to a parent, so no
per-line provenance pass could speak, and six tests then waited out a real
cooldown. Each case below is one variant of that shape.
"""

import sys

import pytest

from tests._resolver_helpers import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / ".github/resolver/auto-resolve"))

from _orphaned_binding import (  # noqa: E402
    describe,
    names_read,
    orphaned_bindings,
    private_module_bindings,
)

_BASE = """\
import time


def wait(seconds):
    time.sleep(seconds)
"""

# The parent that introduced the alias AND pointed the one wait at it.
_OURS = """\
import time

_sleep = time.sleep


def wait(seconds):
    _sleep(seconds)
"""

# The parent that kept the plain call.
_THEIRS = _BASE

# What the merge committed: the alias from one side, the call from the other.
_ORPHANED = """\
import time

_sleep = time.sleep


def wait(seconds):
    time.sleep(seconds)
"""


def test_a_binding_the_merge_left_unread_is_named() -> None:
    assert orphaned_bindings(_BASE, _OURS, _THEIRS, _ORPHANED) == ["_sleep"]


def test_the_parents_own_resolution_is_not_named() -> None:
    """Taking one side whole is always a legal resolution, so neither parent's
    own text may report. `_OURS` reads its alias and `_THEIRS` never binds one."""
    assert orphaned_bindings(_BASE, _OURS, _THEIRS, _OURS) == []
    assert orphaned_bindings(_BASE, _OURS, _THEIRS, _THEIRS) == []


def test_a_name_a_string_reaches_has_a_reader() -> None:
    """`monkeypatch.setattr(mod, "_sleep", ...)` reaches the binding by its
    spelling, so a name only a string names is still read."""
    patched = _ORPHANED + '\n_PATCH_TARGET = "_sleep"\n'
    assert orphaned_bindings(_BASE, _OURS, _THEIRS, patched) == []


def test_a_name_its_own_parent_never_read_is_not_the_merges_fault() -> None:
    """A dead alias that arrived dead is the author's, not the resolution's."""
    dead_on_arrival = """\
import time

_sleep = time.sleep


def wait(seconds):
    time.sleep(seconds)
"""
    assert orphaned_bindings(_BASE, dead_on_arrival, _THEIRS, _ORPHANED) == []


def test_a_binding_the_base_already_carried_is_not_named() -> None:
    """The merge did not introduce it, so the merge did not orphan it."""
    base_with_alias = "import time\n\n_sleep = time.sleep\n"
    assert orphaned_bindings(base_with_alias, _OURS, _THEIRS, _ORPHANED) == []


def test_a_public_name_is_never_named() -> None:
    """An importer this analysis cannot see may read it, so only the leading
    underscore makes zero readers here mean zero readers anywhere."""
    public_ours = _OURS.replace("_sleep", "sleep_for")
    public_orphaned = _ORPHANED.replace("_sleep", "sleep_for")
    assert orphaned_bindings(_BASE, public_ours, _THEIRS, public_orphaned) == []


def test_an_unparseable_side_declines_instead_of_raising() -> None:
    """A side still carrying conflict markers is the ordinary case, and it takes
    the report with it: the parent that read the name is the evidence this
    accusation rests on, so a side nothing can parse leaves it unproven."""
    assert orphaned_bindings(_BASE, "<<<<<<< ours\n", _THEIRS, _ORPHANED) == []
    assert orphaned_bindings(_BASE, _OURS, _THEIRS, "=======\n") == []


def test_an_absent_side_declines_instead_of_raising() -> None:
    """`_blob` answers None for a path a revision does not carry."""
    assert orphaned_bindings(None, _OURS, None, _ORPHANED) == ["_sleep"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("_a = 1\n", {"_a"}),
        ("_a: int = 1\n", {"_a"}),
        ("a = 1\n", set()),
        ("_a, _b = 1, 2\n", set()),
        ("def f():\n    _a = 1\n", set()),
        ("obj._a = 1\n", set()),
    ],
)
def test_private_module_bindings_reads_only_a_plain_module_level_name(
    source: str, expected: set[str]
) -> None:
    import ast

    assert private_module_bindings(ast.parse(source)) == expected


def test_names_read_covers_a_load_and_a_string() -> None:
    import ast

    tree = ast.parse('x = _a\ny = "_b"\n_c = 1\n')
    assert {"_a", "_b"} <= names_read(tree)
    assert "_c" not in names_read(tree)


def test_describe_truncates_with_a_count() -> None:
    assert describe(["_a", "_b"]) == "_a, _b"
    assert describe([f"_n{i}" for i in range(7)]).endswith(", and 2 more")
