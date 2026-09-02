"""Whether a chained child's head already carries a merge commit its base lacks.

PROBLEM CLASS — judging a range from a listing that did not cover it. GitHub's
`compare` serves commits oldest-first and pages them, so a head far ahead of its
base hides exactly the newest commits, which is where a merge from the base sits.
A reader that took one page's `commits` for the whole range read every long chain
as linear, and the caller refused each one it could not characterise.
"""

import json
from collections.abc import Callable

# How much of a chained child's comparison the scan reads. `compare` serves at
# most 250 commits, so three pages of 100 cover every range it answers. The bound
# is explicit rather than inherited from `gh --paginate`, which follows a page
# list of any length and buys neither an answer nor a bounded request count.
COMPARE_PAGE = 100
COMPARE_MAX_PAGES = 3


def carries_a_merge(read_page: Callable[[int], str | None], span: str) -> bool | None:
    """Does the range SPAN (`base...head`) hold a merge commit?

    READ_PAGE answers one page of the comparison as raw JSON, or None when that
    read failed. SPAN also names the range in the warning this prints.

    None when the read could not answer, which the caller treats as a refusal: a
    chain it cannot characterise keeps the old behaviour. A native stack requires
    fully linear history between its layers, so a head carrying ANY merge the base
    lacks is not one, whatever its shape suggests. That makes the answer a sound
    test for "landing one more merge commit here breaks nothing", and it needs no
    stacked-PR API: `compare` serves each commit's parents, and a commit with two
    is a merge.

    Only the False answer needs the whole range: a merge the read DID serve stands
    whatever it missed, so a range past the page bound still answers True when a
    merge sits inside the part that was served.
    """
    commits: list = []
    # An unread reply reports one commit nothing listed, so the completeness test
    # below refuses it rather than answering False. An empty range serves a page,
    # and answers 0 of 0.
    total = 1
    for page in range(1, COMPARE_MAX_PAGES + 1):
        raw = read_page(page)
        if raw is None:
            return None
        payload = json.loads(raw or "{}") or {}
        served = payload.get("commits") or []
        commits += served
        total = payload.get("total_commits", total)
        if any(len(commit.get("parents", ())) >= 2 for commit in served):
            # A merge SEEN answers True whatever the read missed: the question is
            # whether one exists, so no unlisted commit retracts it. Asked before
            # the completeness test below on purpose — a range past the page bound
            # is still answerable when a merge is inside it.
            return True
        if len(commits) >= total or len(served) < COMPARE_PAGE:
            break
    if len(commits) < total:
        # This refusal keeps a short read from answering False, which is the arm
        # licensing a notice that tells an author their head carries no merge.
        print(f"::warning::comparison {span} listed {len(commits)} of {total} commits.")
        return None
    return False
