"""The refusal that keeps a path no edit can resolve out of the model's set."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    classify,
)
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    fail,
)


def refuse_unmergeable(allowed: list[str], base_remote_ref: str) -> None:
    """No unmergeable path (a `-merge`-attributed lockfile, a binary) may sit in
    ALLOWED, the conflicted set; an edit-based resolution of one is unverifiable.

    The verdict is `_paths.classify`'s, which is also what prepare.sh partitions
    with. The two MUST agree: prepare only sends a path here, in CONFLICT_LIST,
    after classifying it as mergeable, so a second reader that answered
    differently would refuse a run prepare had already judged safe.
    """
    for name, facts in classify(allowed, base_remote_ref=base_remote_ref).items():
        if facts.lockfile:
            fail(
                f"the recognized lockfile '{name}' reached CONFLICT_LIST",
                f"`{name}` is a lockfile, so the only correct resolution is "
                "re-running its lock command against the merged manifest. "
                "The routing pass should never have handed it to a model.",
                resolver_fault=True,
            )
        if facts.unmergeable:
            fail(
                f"unmergeable (lockfile/binary) path '{name}' in CONFLICT_LIST",
                f"`{name}` cannot be merged textually; resolve it by hand "
                "(e.g. re-run the lockfile tool after merging).",
            )
