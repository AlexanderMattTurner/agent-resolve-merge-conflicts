"""The mechanical merge a merge commit's resolution is judged against.

PROBLEM CLASS — reconstruct what git itself would have merged, for a merge
with two parents or more (an octopus), with no merge driver answering.
"""

import subprocess
import sys
from functools import cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "auto-resolve"))
# pylint: disable=wrong-import-position  # must follow the sys.path insert above
from _conflict_hunks import (  # noqa: I001
    MECHANICAL_CONFLICT_STYLE,
    conflict_style_args,
    driver_free_args,
)
from _git_io import git


@cache
def repo_driver_free_args() -> list[str]:
    """The driver overrides for THIS repository, read once.

    EVERY command here that re-runs the merge takes them — `merge-tree` and each
    `git show --remerge-diff` alike. A checkout that registered a driver answers
    through it, so the resolve job, which installs mergiraf and binds it, saw a
    delta list with each mergiraf-resolved file already missing (agent-glovebox
    #6012). Overriding one command and not the others is worse than overriding
    none: the sections would come from the driver's merge while the retirement
    passes judge them against git's.
    """
    # Unchecked: `git config --get-regexp` exits 1 when nothing matches, which
    # is the ordinary answer.
    listing = subprocess.run(
        # cwd-git-ok: the configuration that decides this merge is the one the
        # merge runs under, which is this repository's.
        ["git", "config", "--get-regexp", r"^merge\..*\.driver$"],
        capture_output=True,
        text=True,
        check=False,
    )
    return driver_free_args(listing.stdout)


def mechanical_tree(parent1: str, parent2: str) -> tuple[str, list[str]]:
    """The mechanical 3-way merge of two parents as a tree oid, and git's own
    messages when that merge conflicted. Never cached on the parent shas: the
    oid names a loose object in the repository this ran in, and two
    repositories can hold the same commit sha for the same content."""
    res = subprocess.run(
        # cwd-git-ok: the repository under report IS the one this runs in — every
        # other read here resolves the same way, and --write-tree only adds loose
        # objects to it
        [
            "git",
            *conflict_style_args(MECHANICAL_CONFLICT_STYLE),
            *repo_driver_free_args(),
            "merge-tree",
            "--write-tree",
            parent1,
            parent2,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    tree = res.stdout.split("\n", 1)[0]
    # Exit 1 is git's conflicted-but-written verdict; anything else must fail
    # loud, or a garbage tree here would mark every delta superseded.
    if res.returncode not in (0, 1) or not tree:
        raise SystemExit(
            f"git merge-tree --write-tree {parent1} {parent2} failed: "
            f"{res.stderr.strip()}"
        )
    # Exit 1 is the conflict; git prints its messages after the first blank line.
    return tree, res.stdout.partition("\n\n")[2].splitlines() if res.returncode else []


def _commit_tree(tree: str, *parents: str) -> str:
    """A new commit of TREE on PARENTS. It is a loose object no ref names."""
    return git(
        "-c",
        "user.name=remerge-diff-report",
        "-c",
        "user.email=remerge-diff-report@invalid",
        "commit-tree",
        "--no-gpg-sign",
        tree,
        *(arg for parent in parents for arg in ("-p", parent)),
        "-m",
        "remerge-diff-report: octopus fold",
    ).strip()


def two_parent_view(parents: list[str]) -> tuple[list[str], list[str]]:
    """`parents` (a merge, then each commit it merged) as `[merge, first, last]`
    for a two-parent merge, and git's messages for every conflicted fold step.

    `--remerge-diff` cannot reconstruct an octopus. Git's octopus strategy folds
    the parents left to right, each step a 3-way merge of the result so far and
    the next parent. FIRST is a built commit holding the fold of every parent
    but the last, and MERGE a built commit of the merge's tree on FIRST and
    LAST. Its remerge-diff is then the octopus's own final fold step. A
    two-parent merge folds nothing and is its own view.

    That strategy refuses on any conflicted step, so a returned message means a
    person wrote the merge. The caller must render it, whatever the diff says.
    """
    merge, first, *rest = parents
    if len(rest) == 1:
        return parents, []
    conflicts: list[str] = []
    for number, nxt in enumerate(rest, 2):
        tree, messages = mechanical_tree(first, nxt)
        conflicts += [f"folding in parent {number}: {line}" for line in messages]
        if number < len(parents) - 1:
            first = _commit_tree(tree, first, nxt)
    return [
        _commit_tree(f"{merge}^{{tree}}", first, rest[-1]),
        first,
        rest[-1],
    ], conflicts
