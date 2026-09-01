"""The refusal that keeps a path no edit can resolve out of the model's set."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    git,
)
from _lockfiles import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    rule_for as lockfile_rule_for,
)
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    fail,
)


def is_unmergeable(path: str, base_remote_ref: str) -> bool:
    """A path no edit can resolve: `-merge`-attributed, or binary to git.

    The attribute is read from BASE_REMOTE_REF, not the worktree, matching
    prepare.sh's `is_unmergeable` (lib.sh) — the two must agree on the same
    path, since prepare only sends a path here (in CONFLICT_LIST) after
    classifying it as mergeable. Reading the worktree's `.gitattributes`
    instead would judge PRs whose branch still carries an attribute the base
    already removed, which mismatches prepare's now base-derived verdict."""
    if (
        git("check-attr", f"--source={base_remote_ref}", "merge", "--", path)
        .strip()
        .endswith(": merge: unset")
    ):
        return True
    numstat = git("diff", "--numstat", "HEAD", "MERGE_HEAD", "--", path)
    return numstat.split("\t")[0] == "-" if numstat else False


def refuse_unmergeable(allowed: list[str], base_remote_ref: str) -> None:
    """No unmergeable path (a `-merge`-attributed lockfile, a binary) may sit in
    ALLOWED, the conflicted set; an edit-based resolution of one is unverifiable."""
    for name in allowed:
        if lockfile_rule_for(name) is not None:
            fail(
                f"the recognized lockfile '{name}' reached CONFLICT_LIST",
                f"`{name}` is a lockfile, so the only correct resolution is "
                "re-running its lock command against the merged manifest. "
                "The routing pass should never have handed it to a model.",
                resolver_fault=True,
            )
        if is_unmergeable(name, base_remote_ref):
            fail(
                f"unmergeable (lockfile/binary) path '{name}' in CONFLICT_LIST",
                f"`{name}` cannot be merged textually; resolve it by hand "
                "(e.g. re-run the lockfile tool after merging).",
            )
