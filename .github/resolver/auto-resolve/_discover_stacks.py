"""Which open pull requests GitHub lists as layers of a native stack.

PROBLEM CLASS — telling a native stack from a hand-wired chain. Both give a pull
request whose base is another open pull request's head, and they want opposite
handling: a native stack's cascading rebase owns its layers' conflicts, while only
this resolver lands a hand-wired chain's, by merging the parent into the child.
The branch history cannot tell them apart, because a freshly forked layer of
either shape is linear. GitHub can: `GET /repos/{owner}/{repo}/stacks` lists every
stack it holds, each with its member pull requests and their state.
"""

import json
from typing import Protocol

# How much of the stack listing the scan reads. The listing has no state filter,
# so it holds every stack the repository ever made. The bound is explicit rather
# than inherited from `gh --paginate`, which follows a page list of any length.
STACKS_PAGE = 100
STACKS_MAX_PAGES = 10


class PageReader(Protocol):
    """One page of the stack listing as raw JSON, or None when the read failed."""

    def __call__(self, page: int) -> str | None: ...


def open_stack_members(read_page: PageReader) -> frozenset[int] | None:
    """The number of every OPEN pull request some native stack lists as a member.

    None when the listing could not be read whole: a failed page, or a listing
    that still fills its last page at the bound. The caller refuses every chained
    child on None, because a stack this read missed may hold one of them.
    """
    members: set[int] = set()
    for page in range(1, STACKS_MAX_PAGES + 1):
        raw = read_page(page)
        if raw is None:
            return None
        stacks = json.loads(raw or "[]") or []
        members |= {
            member["number"]
            for stack in stacks
            for member in stack.get("pull_requests") or ()
            if member.get("state") == "open"
        }
        if len(stacks) < STACKS_PAGE:
            return frozenset(members)
    print(
        f"::warning::the native-stack listing still filled page {STACKS_MAX_PAGES} "
        f"of {STACKS_PAGE} stacks, so it was not read whole."
    )
    return None
