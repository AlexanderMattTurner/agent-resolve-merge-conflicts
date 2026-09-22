"""The merge kept a shell call whose definition the other parent deleted, in the
same file and across two.

covers: .github/resolver/auto-resolve/_undefined_command.py
covers: .github/resolver/lib_bash_ast.py

The motivating merge is this repository's #149: `prepare.sh` kept a nested
`if is_modify_delete "$f"` from one parent while taking the other's deletion of
that helper. This check is a heuristic like its siblings, so the FILTERS below
are its contract — a case here that stops firing ships a false positive into
every resolution, and one that starts firing hides the break it exists to name.
"""

import subprocess

import pytest

from tests._resolver_helpers import load_script

undefined_command = load_script(".github/resolver/auto-resolve/_undefined_command.py")
undefined_calls = undefined_command.undefined_calls
defined_functions = undefined_command.defined_functions
called_names = undefined_command.called_names
shell_seams = undefined_command.shell_seams
dropped_definition_seams = undefined_command.dropped_definition_seams
is_shell = undefined_command.is_shell
available_names = undefined_command.available_names

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
    guessing which function the variable holds. Asserted on the READER: the
    intersection in `undefined_calls` empties for any unknown name, so a
    broken expansion reading would pass there for the wrong reason."""
    assert called_names('"$helper" x\n') == set()


def test_a_wrapper_prefix_names_the_command_it_decorates() -> None:
    """`command f` runs `f`. Reading the prefix as the call misses #149's own
    shape written that way, and `lib_bash_ast` is where the unwrapping lives so
    the pipefail lint and this check agree on what a stage invokes."""
    assert "is_modify_delete" in called_names('command is_modify_delete "$f"\n')


def test_a_side_the_grammar_cannot_read_whole_declines_the_comparison() -> None:
    """A parent still carrying a conflict marker parses to an ERROR region, and
    the definitions inside it vanish while the calls around it survive — which
    reads as a drop the merge never made."""
    unreadable = "<<<<<<< HEAD\nis_modify_delete() { :; }\n=======\n"
    assert undefined_calls([unreadable, _BASE], _MERGED) == []


@pytest.mark.parametrize("name", ["grep", "kubectl"], ids=["coreutil", "project-tool"])
def test_deleting_a_wrapper_around_a_real_command_is_not_a_finding(name: str) -> None:
    """A parent that drops `grep() { command grep --color=never "$@"; }` leaves
    every call resolving to the binary. Reporting it would cost a correct
    resolution its auto-merge.

    Both names, because the suppressor reads the merge's own blobs: the body
    naming itself is the evidence the command exists, so a wrapper around a tool
    no list of command names would carry is cleared on the same evidence."""
    call = f"{name} -q x f\n"
    wrapper = f'{name}() {{ command {name} --color=never "$@"; }}\n{call}'
    assert undefined_calls([wrapper, call], call) == []


def test_a_deleted_helper_merely_NAMED_after_a_command_is_still_a_finding() -> None:
    """The refusing direction: a body that calls something else is a helper, not
    a wrapper, so its deletion leaves the call reaching nothing this repository
    defines. Clearing it on the name alone would silence the break."""
    helper = 'cat() { printf "%s" "$1"; }\ncat x\n'
    assert undefined_calls([helper, "cat x\n"], "cat x\n") == ["cat"]


def test_a_top_level_call_above_its_definition_cannot_reach_it() -> None:
    """Bash defines a function when it RUNS the definition, so this exits 127
    just as a deleted helper does. A merge that only reordered the two would
    otherwise pass, because both texts define the name."""
    reordered = 'is_modify_delete "$1"\nis_modify_delete() { :; }\n'
    assert available_names(reordered) == set()
    assert undefined_calls([_HEAD, _BASE], reordered) == ["is_modify_delete"]


def test_a_call_inside_a_function_body_reaches_a_definition_below_it() -> None:
    """The refusing direction: the body runs when the caller is invoked, by
    which time the whole file has been read. Reporting this would fire on the
    ordinary layout where helpers sit under the function that uses them."""
    deferred = 'main() { is_modify_delete "$1"; }\nis_modify_delete() { :; }\nmain\n'
    assert "is_modify_delete" in available_names(deferred)
    assert undefined_calls([_HEAD, _BASE], deferred) == []


def test_both_definition_forms_are_read() -> None:
    """`f() { }` and `function f { }` are the same definition to bash, so a
    parent using either has defined the name."""
    assert defined_functions("function has_fact { :; }\n") == {"has_fact"}
    assert defined_functions("has_fact() { :; }\n") == {"has_fact"}


def test_a_call_reads_the_command_name_not_its_arguments() -> None:
    called = called_names('has_fact "$f" modify_delete\n')
    assert "has_fact" in called
    assert "modify_delete" not in called


def test_a_suffixless_shell_script_is_read(tmp_path, monkeypatch) -> None:
    """The git hooks carry no suffix, and a hook that loses a helper fails
    OPEN — the gate stops running and nothing says so."""
    (tmp_path / "pre-commit").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (tmp_path / "notes").write_text("# is_modify_delete\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert [is_shell(p) for p in ("a.sh", "a.bash", "pre-commit", "notes", "a.py")] == [
        True,
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


def test_a_helper_moved_into_the_keyword_form_is_not_reported(
    tmp_path, monkeypatch
) -> None:
    """`function f { }` is the same definition to bash, and `defined_functions`
    reads it — so the shortlist that feeds the parse has to match it too. A
    pre-filter requiring a `(` shortlists nothing here and reports a correct
    relocation as a break."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "lib.sh").write_text(
        "function is_modify_delete {\n  :\n}\n", encoding="utf-8"
    )
    (tmp_path / "prepare.sh").write_text(_MERGED, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    monkeypatch.chdir(tmp_path)
    assert shell_seams([_HEAD, _BASE], _MERGED, "prepare.sh") == []


# The orphan direction, reduced: one side added the helper and called it, the
# other inlined that work, and the merge kept the definition beside the inline
# body. Nothing in this file calls the helper any more.
_ORPHAN_BASE = 'hello() { echo hi; }\nhello "$1"\n'
_ORPHAN_SIDE = (
    'hello() { echo hi; }\nstop_records() { echo bye; }\nhello "$1"\nstop_records\n'
)
_ORPHAN_OTHER = 'hello() { echo hi; }\nhello "$1"\necho bye\n'
_ORPHANED = 'hello() { echo hi; }\nstop_records() { echo bye; }\nhello "$1"\necho bye\n'


def _orphan_repo(tmp_path, monkeypatch, caller: str, body: str) -> None:
    """A tracked repository holding the orphaned resolution and one other file."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "prepare.sh").write_text(_ORPHANED, encoding="utf-8")
    (tmp_path / caller).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / caller).write_text(body, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    monkeypatch.chdir(tmp_path)


def test_an_extensionless_shebang_script_still_counts_as_a_caller(
    tmp_path, monkeypatch
) -> None:
    """A tracked script with no suffix is shell source `is_shell` accepts, so a
    caller search that skipped it would call a still-called helper orphaned,
    spend a repair pass on it and turn auto-merge off."""
    _orphan_repo(
        tmp_path, monkeypatch, "bin/deploy", "#!/usr/bin/env bash\nstop_records\n"
    )
    assert (
        undefined_command.orphaned_definitions(
            _ORPHAN_BASE, [_ORPHAN_SIDE, _ORPHAN_OTHER], _ORPHANED, "prepare.sh"
        )
        == []
    )


def test_an_extensionless_file_that_is_not_shell_clears_nothing(
    tmp_path, monkeypatch
) -> None:
    """The refusing direction: the same name in a suffixless file with no shell
    shebang. Without this the test above passes against a search that reads every
    file in the tree as bash."""
    _orphan_repo(tmp_path, monkeypatch, "NOTES", "stop_records is gone\n")
    assert undefined_command.orphaned_definitions(
        _ORPHAN_BASE, [_ORPHAN_SIDE, _ORPHAN_OTHER], _ORPHANED, "prepare.sh"
    ) == ["stop_records"]


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


# agent-glovebox#6940, reduced. The base added a guard to the library and a call
# to it in the script that sources it; the branch replaced the library's
# contents, and the resolution took the branch's copy. The caller merged clean,
# so no conflict named the break and `create --clone` exited 127 on every shard.
_LIB_BASE = 'kata_clone_source_check() { [[ -e "$1" ]]; }\n'
_LIB_HEAD = "kata_clone_disk() { : ; }\n"
_CALLER = (
    "#!/usr/bin/env bash\nsource lib/clone.bash\n"
    'kata_clone_source_check "$1" || exit 1\n'
)


def _seam_repo(tmp_path, monkeypatch, merged: str, caller: str, **others: str) -> None:
    """A tracked repository holding the merged library, its caller, and OTHERS.

    Both tree searches run `git grep`, so every file has to be added."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib/clone.bash").write_text(merged, encoding="utf-8")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/gb-kata-vm").write_text(caller, encoding="utf-8")
    for name, body in others.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    monkeypatch.chdir(tmp_path)


def test_a_definition_dropped_from_one_file_that_another_still_calls(
    tmp_path, monkeypatch
) -> None:
    """The merge this check exists for. `shell_seams` reads the library's own
    call sites and finds none, so before this the resolution landed clean."""
    _seam_repo(tmp_path, monkeypatch, _LIB_HEAD, _CALLER)
    assert shell_seams([_LIB_HEAD, _LIB_BASE], _LIB_HEAD, "lib/clone.bash") == []
    assert dropped_definition_seams(
        [_LIB_HEAD, _LIB_BASE], _LIB_HEAD, "lib/clone.bash"
    ) == ["kata_clone_source_check"]


def test_a_definition_the_merged_tree_still_binds_elsewhere_is_not_reported(
    tmp_path, monkeypatch
) -> None:
    """The legitimate refactor: the parent moved the guard into another library
    rather than deleting it, so the surviving call still resolves."""
    _seam_repo(
        tmp_path,
        monkeypatch,
        _LIB_HEAD,
        _CALLER,
        **{"lib/guard.bash": _LIB_BASE},
    )
    assert (
        dropped_definition_seams([_LIB_HEAD, _LIB_BASE], _LIB_HEAD, "lib/clone.bash")
        == []
    )


def test_a_definition_nothing_in_the_merged_tree_calls_is_not_reported(
    tmp_path, monkeypatch
) -> None:
    """A helper deleted along with every call to it is a deliberate removal. A
    definition with no caller is `orphaned_definitions`' question, not this
    one, and reporting it here would fire on every helper a branch retires."""
    _seam_repo(tmp_path, monkeypatch, _LIB_HEAD, "#!/usr/bin/env bash\necho hi\n")
    assert (
        dropped_definition_seams([_LIB_HEAD, _LIB_BASE], _LIB_HEAD, "lib/clone.bash")
        == []
    )


def test_a_name_another_file_only_mentions_is_not_a_surviving_call(
    tmp_path, monkeypatch
) -> None:
    """The refusing direction for the test above: the caller search PARSES the
    other file, so a comment or a string holding the name clears nothing and
    accuses nothing."""
    _seam_repo(
        tmp_path,
        monkeypatch,
        _LIB_HEAD,
        "#!/usr/bin/env bash\n# kata_clone_source_check ran here once\n",
    )
    assert (
        dropped_definition_seams([_LIB_HEAD, _LIB_BASE], _LIB_HEAD, "lib/clone.bash")
        == []
    )


def test_a_call_in_the_file_that_lost_the_definition_is_reported_once(
    tmp_path, monkeypatch
) -> None:
    """`shell_seams` already names a call the merged file itself left, so this
    check stands down on that name. Two records for one break would spend two
    of `land`'s bullets and read as two findings."""
    merged = _LIB_HEAD + 'kata_clone_source_check "$1"\n'
    _seam_repo(tmp_path, monkeypatch, merged, _CALLER)
    assert shell_seams([_LIB_HEAD, _LIB_BASE], merged, "lib/clone.bash") == [
        "kata_clone_source_check"
    ]
    assert (
        dropped_definition_seams([_LIB_HEAD, _LIB_BASE], merged, "lib/clone.bash") == []
    )


def test_dropping_a_wrapper_around_a_real_command_is_not_a_finding(
    tmp_path, monkeypatch
) -> None:
    """Dropping `grep() { command grep …; }` restores the command, so every
    surviving call still runs. A finding here costs a correct resolution its
    auto-merge."""
    wrapper = 'grep() { command grep --color=never "$@"; }\n'
    _seam_repo(
        tmp_path, monkeypatch, _LIB_HEAD, '#!/usr/bin/env bash\ngrep -q x "$1"\n'
    )
    assert (
        dropped_definition_seams([_LIB_HEAD, wrapper], _LIB_HEAD, "lib/clone.bash")
        == []
    )


def test_a_side_the_grammar_cannot_read_whole_declines_the_cross_file_check(
    tmp_path, monkeypatch
) -> None:
    """An unreadable side yields no definitions, which reads as a drop. Every
    name the other side binds would then be reported as lost."""
    _seam_repo(tmp_path, monkeypatch, _LIB_HEAD, _CALLER)
    assert (
        dropped_definition_seams(
            [_LIB_HEAD, _LIB_BASE + "if [ "], _LIB_HEAD, "lib/clone.bash"
        )
        == []
    )
