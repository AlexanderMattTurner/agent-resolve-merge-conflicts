"""The merge kept a shell call whose definition the other parent deleted.

covers: .github/resolver/auto-resolve/_undefined_command.py
covers: .github/resolver/lib_bash_ast.py

The motivating merge is this repository's #149: `prepare.sh` kept a nested
`if is_modify_delete "$f"` from one parent while taking the other's deletion of
that helper. This check is a heuristic like its siblings, so the FILTERS below
are its contract — a case here that stops firing ships a false positive into
every resolution, and one that starts firing hides the break it exists to name.
"""

import subprocess

from tests._resolver_helpers import load_script

undefined_command = load_script(".github/resolver/auto-resolve/_undefined_command.py")
undefined_calls = undefined_command.undefined_calls
defined_functions = undefined_command.defined_functions
called_names = undefined_command.called_names
shell_seams = undefined_command.shell_seams
is_shell = undefined_command.is_shell

# #149, reduced. One parent renamed the helper and deleted it; the other added
# the call. Each parent runs; the merge calls a name nothing defines.
_HEAD = """\
is_modify_delete() { [[ -n "$2" ]]; }

for f in "$@"; do
  if is_modify_delete "$f"; then
    keep "$f"
  fi
done
"""
_BASE = """\
has_fact() { [[ -n "$2" ]]; }

for f in "$@"; do
  has_fact "$f" modify_delete
done
"""
_MERGED = """\
has_fact() { [[ -n "$2" ]]; }

for f in "$@"; do
  if is_modify_delete "$f"; then
    keep "$f"
  fi
done
"""


def test_the_merge_that_dropped_a_definition_and_kept_its_call() -> None:
    assert undefined_calls([_HEAD, _BASE], _MERGED) == ["is_modify_delete"]


def test_a_call_whose_definition_survived_is_not_a_finding() -> None:
    """The ordinary resolution: the merge took the helper as well as its call."""
    assert undefined_calls([_HEAD, _BASE], _HEAD) == []


def test_an_external_command_is_not_a_finding() -> None:
    """`keep` and `[[` resolve outside the file, and no parent defined either.
    Only a name a PARENT of this file defined can be one the merge dropped."""
    assert "keep" not in undefined_calls([_HEAD, _BASE], _MERGED)


def test_a_break_both_parents_already_shipped_is_not_this_merge_s() -> None:
    """Neither parent defines the name, so the merge dropped nothing — the call
    was already broken on both branches and this check must stay quiet."""
    broken = 'for f in "$@"; do\n  is_modify_delete "$f"\ndone\n'
    assert undefined_calls([broken, broken], broken) == []


def test_a_name_only_a_comment_or_a_string_mentions_is_not_a_call() -> None:
    """Read from the grammar, not the text: a mention is not an invocation, and
    reporting one would be a false positive on every merge that edits a doc
    comment naming a helper."""
    mentioned = '# is_modify_delete was renamed\necho "is_modify_delete"\n'
    assert undefined_calls([_HEAD, _BASE], mentioned) == []


def test_a_command_name_an_expansion_decides_is_not_judged() -> None:
    """`$helper "$f"` names a command only at run time. Judging it would mean
    guessing which function the variable holds."""
    expanded = 'helper=is_modify_delete\n"$helper" x\n'
    assert undefined_calls([_HEAD, _BASE], expanded) == []


def test_both_definition_forms_are_read() -> None:
    """`f() { }` and `function f { }` are the same definition to bash, so a
    parent using either has defined the name."""
    assert defined_functions("function has_fact { :; }\n") == {"has_fact"}
    assert defined_functions("has_fact() { :; }\n") == {"has_fact"}


def test_a_call_reads_the_command_name_not_its_arguments() -> None:
    called = called_names('has_fact "$f" modify_delete\n')
    assert "has_fact" in called
    assert "modify_delete" not in called


def test_only_shell_suffixes_are_read() -> None:
    assert [is_shell(p) for p in ("a.sh", "a.bash", "a.py", "prepare")] == [
        True,
        True,
        False,
        False,
    ]


def _git(repo, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_a_helper_moved_into_another_file_is_not_reported(
    tmp_path, monkeypatch
) -> None:
    """The legitimate refactor this check must stay silent on: the parent moved
    the helper into a sourced library rather than deleting it, so the surviving
    call still resolves. Suppression is decided by PARSING the other file, not
    by matching its text."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "lib.sh").write_text("is_modify_delete() { :; }\n", encoding="utf-8")
    (tmp_path / "prepare.sh").write_text(_MERGED, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    monkeypatch.chdir(tmp_path)
    assert shell_seams([_HEAD, _BASE], _MERGED, "prepare.sh") == []


def test_a_helper_nothing_else_defines_is_still_reported(tmp_path, monkeypatch) -> None:
    """The refusing direction for the test above: a tree that merely MENTIONS
    the name elsewhere has not defined it, so the finding stands."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "lib.sh").write_text(
        "# is_modify_delete lived here\n", encoding="utf-8"
    )
    (tmp_path / "prepare.sh").write_text(_MERGED, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    monkeypatch.chdir(tmp_path)
    assert shell_seams([_HEAD, _BASE], _MERGED, "prepare.sh") == ["is_modify_delete"]
