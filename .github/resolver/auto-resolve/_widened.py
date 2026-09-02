"""The edits a shard made in files this PR changed, once the declines are known."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    git,
    git_bytes,
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


_WHITESPACE = re.compile(rb"\s+")


def _same_but_for_whitespace(one: bytes, other: bytes) -> bool:
    """Do the two texts differ in whitespace alone? Compared as BYTES, because a
    widened path can hold content this process must not assume it can decode."""
    return _WHITESPACE.sub(b"", one) == _WHITESPACE.sub(b"", other)


def revert_whitespace_only_edits(widened: list[str]) -> list[str]:
    """PROBLEM CLASS — a merge commit carries a line neither parent wrote.

    Put back a widened edit whose whole difference from the merge is whitespace,
    and return what is left. A widened path merged CLEANLY, so the index holds
    the content both parents agree on and a checkout restores it.

    The widening grant exists so a shard can port a definition into a file the
    merge left wrong. Re-spacing one ports nothing: it lands under merge cover,
    where the pull request's own diff does not show it, and a reviewer reads it
    as code someone wrote. On agent-glovebox #5406 and #5408 five such edits
    reached `main` and were reverted by hand.

    A pure re-indentation is reverted too. It changes what Python means, so the
    content both parents wrote is the safer of the two answers."""
    noise = []
    for name in widened:
        if not Path(name).is_file():
            continue
        worktree = Path(name).read_bytes()
        merged = git_bytes("show", f":{name}")
        if worktree != merged and _same_but_for_whitespace(worktree, merged):
            noise.append(name)
    if noise:
        git("checkout", "--", *noise)
        print(
            "::warning::put back the resolver's whitespace-only edit(s) in "
            f"{' '.join(noise)}: re-spacing a file the merge did not conflict on "
            "resolves nothing, and the change lands where this PR's diff does "
            "not show it."
        )
    return [name for name in widened if name not in set(noise)]
