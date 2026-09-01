"""The edits a shard made in files this PR changed, once the declines are known."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    git,
)
from _marker_verdict import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    declined_widened_paths,
)


def settle_widened_edits(widened: list[str]) -> list[str]:
    """Stage WIDENED, minus the paths only a shard that then DECLINED edited, and
    return what was staged.

    A decline keeps the head's whole file, so a companion edit made for it would
    land half a resolution. The index still holds the merge's own content for
    such a path, so a checkout puts it back."""
    dropped = sorted(declined_widened_paths(Path.cwd()) & set(widened))
    if dropped:
        git("checkout", "--", *dropped)
        print(
            "::warning::put back the resolver's edit(s) in "
            f"{' '.join(dropped)}: only a shard that then declined made them."
        )
    kept = [name for name in widened if name not in set(dropped)]
    if kept:
        git("add", "--", *kept)
    return kept
