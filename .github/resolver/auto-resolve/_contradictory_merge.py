"""PROBLEM CLASS — a merge whose every line traces to a parent and whose tree is
still broken, because the lines that survived CONTRADICT each other.

The pre-push merge-delta reviewer refuses a line it cannot trace to one of the two
parents. Both shapes below pass that read, and only a test run finds them
(agent-glovebox #5641):

* a two-sided rename, split. One parent added a module-level alias and pointed its
  one caller at it; the other kept the plain call. The merge kept the alias and the
  plain call, so the alias had zero readers, the fixtures patched a name nothing
  read, and six tests each sat out a real cooldown until pytest-timeout killed them.
* a revert, undone by keeping both sides. Each parent's own commit deleted an
  assertion; the merge put it back beside its negation, and no value satisfies both.

Python only, through `ast` and whole-line comparison — a language with no parser
here is out of scope, never a guess. Every check reads the merge base as well as
both parents, so a finding names a line the MERGE produced rather than a defect
either branch already carried.
"""

import ast
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    git,
    git_result,
)
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    fail,
    report_block,
)
from dropped_name_seams import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    module_level_identifiers,
)

# A resurrected line has to carry enough text that its reappearance means
# something. Below this a merge legitimately repeats `return`, `else:` or a
# closing bracket, and reporting one says nothing about the resolution.
_MIN_RESURRECTED_LINE = 12
# What one refusal quotes. Past this the count carries the signal, and a comment
# that lists every finding of a mangled whole-file merge is unreadable.
_FINDINGS_SHOWN = 10


def _names_read(tree: ast.AST) -> set[str]:
    """Every name TREE reads, in one walk.

    A load of a bare name, a `global` declaration and a string constant all
    count. The last covers the two ways a name is read with no identifier node —
    an `__all__` entry, and a `setattr` by name."""
    read: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            read.add(node.id)
        elif isinstance(node, ast.Global):
            read.update(node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            read.add(node.value)
    return read


def orphaned_added_names(base: str, sides: list[str], merged: str) -> list[str]:
    """The module-level names one of SIDES added AND read, that MERGED still
    defines and never reads.

    Both conditions on the parent are load-bearing. `added` alone reports a name
    written for another module to import; `read by the parent` narrows it to a
    name whose own module used it, so a merged file that defines it and reads it
    nowhere has lost every use of a definition it kept.

    A file that does not parse on any one of the three sides yields nothing: a
    half-parsed comparison misattributes the drop."""
    try:
        base_names = module_level_identifiers(ast.parse(base))
        merged_tree = ast.parse(merged)
    except SyntaxError:
        return []
    orphaned = module_level_identifiers(merged_tree) - _names_read(merged_tree)
    found: set[str] = set()
    for side in sides:
        try:
            side_tree = ast.parse(side)
        except SyntaxError:
            continue
        added = module_level_identifiers(side_tree) - base_names
        found |= added & orphaned & _names_read(side_tree)
    return sorted(found)


def _resurrectable(text: str) -> set[str]:
    """The lines of TEXT whose reappearance in a merge would mean something.

    Stripped, so indentation changes do not hide one. A line has to be long
    enough to be distinctive, carry a word, not be a comment, and appear exactly
    once — a line the file already repeats says nothing about which copy came
    back."""
    seen = Counter(line.strip() for line in text.splitlines())
    return {
        line
        for line, count in seen.items()
        if count == 1
        and len(line) >= _MIN_RESURRECTED_LINE
        and not line.startswith("#")
        and any(char.isalnum() for char in line)
    }


def resurrected_lines(base: str, sides: list[str], merged: str) -> list[str]:
    """The lines BASE holds, that NO side of the merge still holds anywhere, and
    that MERGED brings back.

    Absence from every side is what makes this a resurrection rather than an
    ordinary merge: a line one side still carries traces to that side, and a line
    one side merely MOVED is still somewhere in its blob."""
    gone = _resurrectable(base)
    for side in sides:
        gone -= {line.strip() for line in side.splitlines()}
    return sorted(gone & {line.strip() for line in merged.splitlines()})


class ContradictionReport:
    """The APPLICATION of the two checks above to one bundle step.

    A mixin for the reason `NeitherSideReport` is one: each method reads the
    step's own resolved set and the two parents it merged."""

    def _blob(self, sha: str, name: str) -> str | None:
        """NAME's content at SHA, or None when SHA does not hold it."""
        done = git_result("show", f"{sha}:{name}")
        return done.stdout if done.returncode == 0 else None

    def refuse_a_contradictory_merge(self) -> None:
        """Refuse a resolution whose surviving lines contradict each other.

        A refusal and not a report, unlike the neither-side lines beside it: each
        finding here is a tree that cannot work — a definition with no reader, or
        a line every parent deleted — so landing it spends a full check run to
        report what this already knows.

        Read over the tree as it will be COMMITTED, after the hooks and the
        post-merge repair pass have rewritten what they rewrite."""
        gated = (
            set(self.allowed)
            - set(self.deferred)
            - set(self.modify_delete)
            - set(self.declined)
        )
        paths = sorted(
            name for name in gated if name.endswith(".py") and Path(name).is_file()
        )
        if not paths:
            return
        merge_base = git(
            "merge-base", self.checked_out_head, self.merge_base_side
        ).strip()
        findings: list[str] = []
        for name in paths:
            base = self._blob(merge_base, name)
            sides = [
                blob
                for blob in (
                    self._blob(self.checked_out_head, name),
                    self._blob(self.merge_base_side, name),
                )
                if blob is not None
            ]
            # A path one side ADDED has no base blob to have dropped anything
            # from, and a path with fewer than two sides is not a two-sided
            # resolution at all.
            if base is None or len(sides) < 2:
                continue
            merged = Path(name).read_text(encoding="utf-8")
            findings += [
                f"- `{name}` defines `{orphan}`, which a parent added and read, "
                "and the merged file reads nowhere"
                for orphan in orphaned_added_names(base, sides, merged)
            ]
            findings += [
                f"- `{name}` brings back `{line}`, which every parent's own "
                "commit deleted"
                for line in resurrected_lines(base, sides, merged)
            ]
        if not findings:
            return
        shown = findings[:_FINDINGS_SHOWN]
        rest = len(findings) - len(shown)
        if rest > 0:
            shown.append(f"- and {rest} more")
        fail(
            f"the resolution contradicts itself in {len(findings)} place(s)",
            "the resolution keeps lines from both parents that cannot both be "
            "right: a definition one parent added and used that nothing in the "
            "merged file reads, or a line every parent deleted and this merge "
            "put back. Every line traces to a parent, so the merge-delta review "
            "passes and only a test run would find it.",
            report=report_block("\n".join(shown)),
        )
