"""Whether a chained head carries a merge commit its base lacks.

PROBLEM CLASS — a paged GitHub list read as if one page were the whole answer.
``compare`` serves at most 100 commits per page, OLDEST FIRST, so a head more
than a page ahead of its base hides exactly the newest commits — where a merge
from the base sits. Reading one page answered "100 of 121" and refused every
stacked pull request on a long branch, permanently.

Split out of ``discover.py`` so the paging and GitHub's own 250-commit ceiling
sit in one place rather than inside a scan method.
"""

import json
from collections.abc import Callable
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _discover_types import (  # noqa: E402  # pylint: disable=wrong-import-position
    DiscoverError,
)

# GitHub's own per-page maximum for `compare`. Asking for it keeps the walk below
# to the fewest pages the range allows.
COMPARE_PAGE_SIZE = 100


def chain_carries_a_merge(
    run_gh: Callable[..., str], repo: str, base_ref: str, head_ref: str
) -> bool | None:
    """Does this chain's head hold a merge commit the base does not?

    None when the comparison could not be read or did not cover the range, which
    the caller treats as a refusal — a chain this scan cannot characterise keeps
    the old behaviour.

    A native stack requires fully linear history between its layers, so a head
    carrying ANY merge the base lacks is not one, whatever its shape suggests.
    That makes the answer a sound test for "landing one more merge commit here
    breaks nothing", and it needs no stacked-PR API: `compare` serves each
    commit's parents, and a commit with two is a merge.
    """
    path = f"repos/{repo}/compare/{base_ref}...{head_ref}"
    read = 0
    total = 0
    page = 1
    while True:
        try:
            raw = run_gh(
                ["api", f"{path}?per_page={COMPARE_PAGE_SIZE}&page={page}"],
                capture=True,
            )
        except DiscoverError:
            # Caught rather than propagated: this read decides ONE chained PR, and
            # `run_gh` has already exhausted its retries. Letting it end the scan
            # would drop every other candidate over a PR the rail refuses anyway.
            print(f"::warning::could not compare {base_ref}...{head_ref}.")
            return None
        payload = json.loads(raw)
        commits = payload.get("commits", [])
        total = payload.get("total_commits", read + len(commits))
        if any(len(commit.get("parents", ())) >= 2 for commit in commits):
            return True
        read += len(commits)
        if read >= total or not commits:
            break
        page += 1
    if read < total:
        # GitHub stops serving `compare` at 250 commits however many pages the
        # caller asks for, so a longer range ends here. The refusal is what keeps
        # a short read from answering False, which would post the stacked notice
        # about a head that does carry a merge.
        print(
            f"::warning::comparison {base_ref}...{head_ref} listed "
            f"{read} of {total} commits."
        )
        return None
    return False
