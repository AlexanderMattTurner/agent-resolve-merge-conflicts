"""When the resolver's own code last changed, and how a run says so.

The auto-resolve DISCOVER step reads this for one question: does a handoff mark
still describe the program a re-run would execute? A verdict reached with the
resolver as it stood is a verdict about code that may since have been fixed, and
nothing else in a calling repository lands those conflicts — a mark that ignored
this stranded ten pull requests behind one resolver bug.

Every read fails CLOSED, which here means holding the mark: an unreadable answer
is no evidence of a change, and retrying on one API outage would buy a paid
resolve for every stranded pull request in the scan at once.
"""

import os
import sys

# The code a re-run executes, so a commit to any of it retires every mark older than
# it: a handoff verdict is about THIS program. Read only when the resolver ships in
# the repository it resolves — a caller cloning it from elsewhere asks that clone's
# ref instead, in one call, and never reads this tuple. See resolver_repo_ref.
#
# The whole resolver is ONE directory, and the commits API takes a directory, so the
# list does not enumerate the entry points a resolve stages. What stays beside it is
# the CALLER-side capability each resolve also runs — a change there changes what a
# re-run does, and no read of the resolver directory would see it. A fix outside both
# retires nothing; land it with a touch here, or dispatch with `catch-up=true`.
# What a failed READ answers, kept distinct from the None a path with no commits
# answers: the first holds every mark, the second only says this path is stale.
UNREADABLE = object()

RESOLVER_PATHS = (
    ".github/resolver",
    ".github/workflows/auto-resolve-reusable.yaml",
    ".pre-commit-config.yaml",
    "config/merge-queue-mode.json",
)


def resolver_repo_ref(caller_repo: str) -> tuple[str, str] | None:
    """The repository and ref the resolver is cloned from, or None when it ships
    with the tree being merged.

    The reusable workflow passes both. None on either being empty, and None when
    the two repositories are the same one — there RESOLVER_PATHS is still the
    sharper question, because a commit touching an unrelated file in the caller's
    own repository must not retire a verdict about the resolver.
    """
    repo = os.environ.get("AUTO_RESOLVE_RESOLVER_REPO", "").strip()
    ref = os.environ.get("AUTO_RESOLVE_RESOLVER_REF", "").strip()
    if not repo or not ref or repo == caller_repo:
        return None
    return repo, ref


def resolver_change_source(caller_repo: str) -> str:
    """What a person must move to retire a handoff mark, named the way this run
    reads it — so the skip line points at the tree it actually probed."""
    remote = resolver_repo_ref(caller_repo)
    if remote is not None:
        return f"the ref {remote[0]}@{remote[1]} names"
    return (
        f"any of the {len(RESOLVER_PATHS)} paths in "
        "_discover_resolver_change.py's RESOLVER_PATHS"
    )


def newest_resolver_commit(read_commit_date, repo: str) -> float | None:
    """The newest commit date across the resolver's own code, or None on a failed
    read. READ_COMMIT_DATE answers one API path with an epoch, None when the path
    has no commits, or UNREADABLE when the read itself failed.

    A caller whose resolver lives in ANOTHER repository asks that repository for
    its ref instead, in one call. RESOLVER_PATHS names paths in the tree being
    merged, which is the right question only while the resolver ships with it:
    read against a caller that carries none of them, every path answers "no
    commits", the maximum is empty, and the handoff mark then holds forever on
    every stranded pull request.
    """
    remote = resolver_repo_ref(repo)
    if remote is not None:
        return _ref_committed_at(read_commit_date, *remote)
    dates = []
    for path in RESOLVER_PATHS:
        date = read_commit_date(f"repos/{repo}/commits?path={path}&per_page=1")
        if date is UNREADABLE:
            # A read that FAILED is no evidence of a change, and a partial maximum
            # would claim the resolver did not change since a date this run only
            # partly read.
            return None
        if date is None:
            # 200 with no commits: the path is not on the default branch, so
            # RESOLVER_PATHS is stale. Said out loud, because holding here in
            # silence is what strands a handed-off pull request forever.
            print(
                f"::warning::{path} has no commits on the default branch, so "
                "RESOLVER_PATHS is stale and a change there no longer retires "
                "a handoff mark.",
                file=sys.stderr,
            )
            continue
        dates.append(date)
    return max(dates) if dates else None


def _ref_committed_at(read_commit_date, repo: str, ref: str) -> float | None:
    """When REF in REPO was committed, as an epoch, or None on a failed read.

    The whole resolver arrives from one commit, so its ref's own date is when its
    code last changed — no path list to keep in step with it."""
    date = read_commit_date(f"repos/{repo}/commits/{ref}")
    if date is UNREADABLE:
        return None
    if date is None:
        print(
            f"::warning::{repo}@{ref} carries no commit date, so a change to "
            "the resolver no longer retires a handoff mark.",
            file=sys.stderr,
        )
    return date
