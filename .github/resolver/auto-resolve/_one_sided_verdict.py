"""Which one-sided conflicts a mechanical rule already decides, so no model reads them.

PROBLEM CLASS — a conflict whose answer is derivable, routed to a judgement. Git leaves a
modify/delete with no markers and two possible answers, keep or honour the deletion, so the
resolver asks a model. On four recorded merges the model read the tree, stated the deciding
fact itself, and declined anyway (agent-glovebox#5889). `auto-resolve/declined` has no
automatic exit, so each of those pull requests then waited for a person.

Every rule here honours the DELETION, and none of them ever answers `keep`. That asymmetry is
the safety argument: a rule that fires wrongly drops a file, which the merged tree's own checks
see, while a rule that answered `keep` wrongly would restore a mechanism the other side retired
on purpose and no check would name it.

A rule holds or it does not. Nothing here weighs one against another, so a path no rule matches
reaches the model exactly as it does today.
"""

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    GitCallFailed,
    bind_repo,
    git,
    git_result,
    git_status,
)
from _paths import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Shape,
    Stages,
    unmerged_stages,
)

#: A changelog fragment's text is only evidence of release when it is long enough to name the
#: change. A short line ("Fixed a typo.") appears in many releases, and a match on one would
#: read an unrelated entry as this fragment's own.
_FRAGMENT_MIN_EVIDENCE = 24

#: The leading markdown a fragment's first line carries, stripped before the search so a
#: fragment written as a list item still matches the same text assembled under a heading.
_FRAGMENT_LEAD = re.compile(r"^[\s>*+-]*(?:\d+\.)?\s*")

_FRAGMENT_PATH = re.compile(r"^changelog\.d/.+\.md$")


@dataclass(frozen=True, kw_only=True, slots=True)
class Sides:
    """Which ref holds the surviving version of a one-sided path, and which deleted it.

    `git merge` leaves HEAD as ours and MERGE_HEAD as theirs, so the stage the index kept
    names the survivor. Read per path: one merge can carry a deletion from each direction.
    """

    survivor: str
    deleter: str


@dataclass(frozen=True, kw_only=True, slots=True)
class Context:
    """One modify/delete path, and everything a rule may read about it."""

    path: str
    sides: Sides
    merge_base: str
    #: Every path this pass could DELETE — the modify/delete candidates alone, never the whole
    #: conflicted set. A rule searching for references to `path` excludes them, because a
    #: module and its test are typically deleted by the same commit and each names the other.
    #: A both-modified conflicted path SURVIVES the merge, so excluding it would hide the one
    #: live caller a conflict happens to sit in and delete a module something still imports.
    deciding: frozenset[str]


@dataclass(frozen=True, kw_only=True, slots=True)
class Decision:
    """A rule's answer, and the sentence the pre-pass prints to justify it."""

    rule: str
    evidence: str


def _grep_lines(*args: str) -> list[str]:
    """`git grep` output as lines. Exit 1 is the empty answer; anything above it RAISES.

    `git grep` spends exit 1 on the ordinary answer "nothing matched", which is the case these
    rules exist to find, so `git_lines` raising on it would crash every firing. Exit 2 and up
    is a real fault — a bad pathspec, an unreadable object — and reading THAT as "nothing
    names this path" fails open toward a `git rm`, so it must not reach a rule as an answer.
    """
    done = git_result("grep", *args)
    if done.returncode > 1:
        raise GitCallFailed(["git", "grep", *args], done.returncode, done.stderr)
    return [line for line in done.stdout.splitlines() if line]


def _content(ref: str, path: str) -> str | None:
    """PATH's text at REF, or None when REF does not hold it or it is not text."""
    if git_status("cat-file", "-e", f"{ref}:{path}") != 0:
        return None
    try:
        return git("show", f"{ref}:{path}")
    except UnicodeDecodeError:
        return None


def _reference_spellings(path: str) -> list[tuple[str, bool]]:
    """How a caller could name PATH — each spelling, and whether to bound it to whole words.

    The full path, the basename, and the bare stem in both separator spellings. The stem is
    what an import writes — `import check_closure_python`, or the extensionless `./foo` a JS
    or TS caller uses — so it is searched with WORD BOUNDARIES: unbounded, a stem like `utils`
    matches inside unrelated words and reports a reference nobody wrote.
    """
    name = path.rsplit("/", 1)[-1]
    spellings = [(path, False), (name, False)]
    stem = name.rsplit(".", 1)[0]
    # Both spellings of the stem, never one in place of the other: a shell wrapper or a docs
    # table cites `check-closure-python`, while an import writes `check_closure_python`. The
    # bare stem also covers the extensionless import a JS or TS caller writes — `./foo` for
    # `foo.ts` — which is the only way such a caller ever names the module.
    for bare in {stem, stem.replace("-", "_")}:
        if bare and bare != name:
            spellings.append((bare, True))
    return spellings


def _referenced_at(ref: str, ctx: Context) -> str | None:
    """The first file at REF that names `ctx.path`, or None when nothing does.

    Excludes every path this pass is deciding, per `Context.deciding` — which covers the
    searched path itself, since a module's own docstring and error messages spell its name and
    reading those as a caller would keep every dead module alive.
    """
    excluded = [f":(exclude,literal){name}" for name in sorted(ctx.deciding)]
    for spelling, whole_word in _reference_spellings(ctx.path):
        word = ["-w"] if whole_word else []
        found = _grep_lines(
            "-l", "-F", *word, "-e", spelling, ref, "--", ".", *excluded
        )
        if found:
            # `git grep <ref>` prefixes each name with `<ref>:`; the caller only reports it.
            return found[0].split(":", 1)[-1]
    return None


def _released_changelog_fragment(
    ctx: Context, _decided: dict[str, "Decision"]
) -> str | None:
    """A changelog fragment whose text the deleting side has already shipped.

    A release folds each fragment into `CHANGELOG.md` and deletes it. The surviving side's
    edit is then a reword of a published entry, which is an edit to an audit record.
    """
    if not _FRAGMENT_PATH.match(ctx.path):
        return None
    # TWO premises, because either alone is wrong. The BASE's lines having shipped is what
    # says a release consumed this fragment, so the file has no future. The SURVIVOR adding no
    # line beyond them is what says nothing is lost — a branch that APPENDS a bullet describes
    # a change the release never carried, and deleting would drop it unexamined.
    base_text = _content(ctx.merge_base, ctx.path)
    base_entries = _entries(base_text)
    released = _content(ctx.sides.deleter, "CHANGELOG.md")
    if not base_entries or released is None:
        return None
    if any(entry not in released for entry in base_entries):
        return None
    # EVERY non-blank line for this premise, not `_entries`. That threshold asks whether text
    # is evidence a release shipped it; this asks whether the survivor WROTE a line the release
    # never carried, and a short bullet — `- Also fix the flag.` — is exactly such a line while
    # being too short to count as evidence.
    if len(_bullets(_content(ctx.sides.survivor, ctx.path))) > len(_bullets(base_text)):
        return None
    return (
        f"the deleting side's CHANGELOG.md already carries this fragment's released text "
        f"({base_entries[0][:60]!r}), and the surviving side adds no entry beyond it, so its "
        "edit rewords a record that has already shipped"
    )


def _bullets(text: str | None) -> list[str]:
    """Every non-blank line of TEXT, stripped of a list item's markdown.

    The count this feeds asks whether the surviving side ADDED a line, so it must not skip a
    short one — unlike `_entries`, whose threshold is about evidence.
    """
    if text is None:
        return []
    return [
        entry
        for line in text.splitlines()
        if (entry := _FRAGMENT_LEAD.sub("", line).strip())
    ]


def _entries(text: str | None) -> list[str]:
    """TEXT's substantive changelog lines, stripped of the markdown a list item carries.

    A short line ("Fixed a typo.") appears in many releases, so one is no evidence that THIS
    fragment shipped and is left out.
    """
    if text is None:
        return []
    return [
        entry
        for line in text.splitlines()
        if len(entry := _FRAGMENT_LEAD.sub("", line).strip()) >= _FRAGMENT_MIN_EVIDENCE
    ]


#: How many siblings the glob probe reads before it gives up looking for one referenced by
#: name. A directory whose first few files are all unreferenced is one a glob consumes.
_SIBLING_SAMPLE = 20

#: Prefixes NO rule here decides, whatever it would answer. The module's safety argument is
#: that a wrong deletion goes red in the merged tree's own checks, and each entry is a place
#: that argument fails. Applied in `decide` rather than inside one rule, so every present and
#: future row inherits it.
#: - `.github/workflows/`: a deleted required check reports nothing and hangs the pull request
#:   at "Expected — Waiting" instead of going red. Sibling workflow names ARE cited in docs and
#:   in workflow_run listeners, so the reference rule's own probe does not catch this.
_NEVER_DECIDED = (".github/workflows/",)


def _siblings_at(ref: str, ctx: Context) -> list[str]:
    """The other FILES in `ctx.path`'s directory at REF, excluding the set being decided.

    Blobs only. A subdirectory's path is a substring of every path under it, so one mention of
    `tests/fixtures/shared/x.json` would make the `tests/fixtures` TREE look reached by name
    and unlock the reference rule for every fixture beside it.
    """
    directory = ctx.path.rsplit("/", 1)[0] if "/" in ctx.path else ""
    listed = git(
        "ls-tree", "-z", ref, f"{directory}/" if directory else ".", check=False
    ).split("\0")
    names = []
    for record in listed:
        if not record or "\t" not in record:
            continue
        meta, name = record.split("\t", 1)
        if meta.split()[1] != "blob":
            continue
        if name != ctx.path and name not in ctx.deciding:
            names.append(name)
    return names[:_SIBLING_SAMPLE]


def _reachable_by_name(ref: str, ctx: Context) -> bool:
    """Whether MOST of `ctx.path`'s siblings at REF are named somewhere.

    A directory a glob consumes holds files nobody ever names: `changelog.d/` is read whole by
    the release assembler, and every fragment in it is unreferenced by construction. Without
    this probe the reference rule would call each of them dead and delete a fragment a branch
    had just written.

    A MAJORITY, never one hit. A mixed directory holds an explicitly launched `plugins/bar.py`
    beside a loader of `plugins/*.py`, and one named sibling there would license deleting every
    glob-loaded file around it. Most files being named is what says the directory's convention
    is naming rather than discovery.
    """
    siblings = _siblings_at(ref, ctx)
    if not siblings:
        return False
    named = 0
    for sibling in siblings:
        for spelling, whole_word in _reference_spellings(sibling):
            word = ["-w"] if whole_word else []
            excluded = [
                f":(exclude,literal){name}" for name in sorted(ctx.deciding | {sibling})
            ]
            if _grep_lines(
                "-l", "-F", *word, "-e", spelling, ref, "--", ".", *excluded
            ):
                named += 1
                break
    return named * 2 >= len(siblings)


def _unreferenced_on_both_sides(
    ctx: Context, _decided: dict[str, "Decision"]
) -> str | None:
    """Nothing on EITHER side names this path, so the surviving side's edits reach no caller.

    Both sides, never the deleting side alone: a deletion removes its own callers, so
    "unreferenced after the deletion" is what deleting means and would fire on every
    modify/delete. Asking the SURVIVOR is what makes this evidence — it says the branch still
    editing the file had already stopped calling it.
    """
    if not _reachable_by_name(ctx.sides.survivor, ctx):
        return None
    if _referenced_at(ctx.sides.survivor, ctx) is not None:
        return None
    if _referenced_at(ctx.sides.deleter, ctx) is not None:
        return None
    return (
        "no file on either side names this path, and its neighbours ARE reached by name, so "
        "the surviving side's edits reach no caller on its own branch"
    )


#: The shortest stem that identifies a subject. Below this a name like `a` sits inside most
#: other names, so the match would be a coincidence rather than a derivation.
_MIN_SUBJECT_STEM = 4

#: How a test names the thing it exercises, as the subject's stem fills `{subject}`. ANCHORED:
#: an unanchored `subject in path` reads `scorecard` as a test of `core` and deletes it.
_TEST_OF = ("test_{subject}", "{subject}_test", "test-{subject}", "{subject}-test")


def _stem(path: str) -> str:
    """PATH's basename without its suffix, with `-` and `_` read as the same separator.

    A module spelled with dashes is tested by a file spelled with underscores, so the two
    names only correspond once both are normalised.
    """
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("-", "_")


def _tests_subject(path: str, subject: str) -> bool:
    """Whether PATH is named as a test OF SUBJECT, rather than merely containing its name."""
    stem = _stem(path)
    return any(stem == _stem(shape.format(subject=subject)) for shape in _TEST_OF)


def _follows_a_deleted_subject(
    ctx: Context, decided: dict[str, "Decision"]
) -> str | None:
    """A test whose subject this same pass is deleting, which nothing else references.

    This does not rest on reference counting, so it reaches the case that bound cannot: a test
    directory is consumed by a collector's glob, and every file in it is unreferenced by name.
    What decides it is that the file exists to exercise ONE subject, and that subject is
    leaving the tree — a test of nothing fails or vacuously passes wherever it lands.
    """
    subjects = [
        name
        for name in decided
        if len(_stem(name)) >= _MIN_SUBJECT_STEM
        and _tests_subject(ctx.path, _stem(name))
    ]
    if not subjects:
        return None
    if _referenced_at(ctx.sides.survivor, ctx) is not None:
        return None
    return (
        f"this path is named as a test of {subjects[0]}, which this pass deletes, and nothing "
        "else names it, so its whole subject is leaving the tree"
    )


@dataclass(frozen=True, kw_only=True, slots=True)
class DeleteRule:
    """One mechanical reason to honour a deletion.

    `reads_decided` says the rule needs the other rules' answers, so `decide` runs it in a
    second pass. It is a field rather than a separate list because this table is what a reader
    consults to learn the whole procedure, and a rule that ran outside it would not be here.
    """

    name: str
    holds: Callable[[Context, dict[str, "Decision"]], str | None]
    reads_decided: bool = False


#: The whole decision procedure, in order. Adding a reason is adding a row here and nothing
#: else; a row that reads what the earlier rows decided says so with `reads_decided`.
DELETE_RULES: tuple[DeleteRule, ...] = (
    DeleteRule(name="released-changelog-fragment", holds=_released_changelog_fragment),
    DeleteRule(name="unreferenced-on-both-sides", holds=_unreferenced_on_both_sides),
    DeleteRule(
        name="follows-a-deleted-subject",
        holds=_follows_a_deleted_subject,
        reads_decided=True,
    ),
)


def _sides(stages: Stages) -> Sides | None:
    """Which side survived, or None when this shape names no deletion.

    `ADDED_BY_US` and `ADDED_BY_THEM` are one-sided too, and neither carries a deletion to
    honour: the other side simply never held the path. No rule here applies to them.
    """
    if stages.shape is not Shape.MODIFY_DELETE:
        return None
    if stages.ours is not None:
        return Sides(survivor="HEAD", deleter="MERGE_HEAD")
    return Sides(survivor="MERGE_HEAD", deleter="HEAD")


def decide(paths: list[str]) -> dict[str, Decision]:
    """The subset of PATHS a rule decides, with the rule and its evidence.

    A path no rule matches is absent from the answer and reaches the model unchanged. Callable
    only mid-merge: the stages and MERGE_HEAD it reads exist nowhere else.
    """
    if not paths or git_status("rev-parse", "-q", "--verify", "MERGE_HEAD") != 0:
        return {}
    stages = unmerged_stages()
    merge_base = git("merge-base", "HEAD", "MERGE_HEAD").strip()
    candidates = {
        path: sides
        for path in paths
        if path in stages
        and not path.startswith(_NEVER_DECIDED)
        and (sides := _sides(stages[path])) is not None
    }
    deciding = frozenset(candidates)
    decided: dict[str, Decision] = {}
    contexts = {
        path: Context(path=path, sides=sides, merge_base=merge_base, deciding=deciding)
        for path, sides in candidates.items()
    }
    # Two passes over the ONE table: a rule reading what the others decided cannot answer
    # until they have. Within a pass the first rule that holds wins.
    for second_pass in (False, True):
        # A SNAPSHOT, so a path this pass decides cannot become a subject for a later path in
        # the same pass. Reading the live dict makes the answer depend on the input order and
        # lets one deletion cascade: a.py, then test_a.py, then test_a_helper.py.
        settled = dict(decided)
        for path, ctx in contexts.items():
            if path in decided:
                continue
            for rule in DELETE_RULES:
                if rule.reads_decided is not second_pass:
                    continue
                evidence = rule.holds(ctx, settled)
                if evidence is not None:
                    decided[path] = Decision(rule=rule.name, evidence=evidence)
                    break
    return decided


def main() -> None:
    """One NUL-terminated `path`, `rule` then `evidence` record per decided path.

    Only the decided paths appear, so the shell reads the answer as the set to stage. NUL for
    the reason `_paths.py` gives: a tab-separated record cannot carry a name with whitespace.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()

    bind_repo(args.root)
    decided = decide(args.paths)
    sys.stdout.write(
        "".join(
            f"{path}\0{found.rule}\0{found.evidence}\0"
            for path, found in decided.items()
        )
    )


if __name__ == "__main__":
    main()
