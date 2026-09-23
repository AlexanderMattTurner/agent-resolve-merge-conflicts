#!/usr/bin/env python3
"""Compute merge-conflict verdicts locally, for PRs GitHub's own mergeability
query still answers UNKNOWN or answers against a stale base.

label-merge-conflicts.sh polls GitHub's lazily-computed `mergeable` field and
gives up after a bounded number of passes. `git merge-tree` is git's own
three-way merge: it needs no round trip to GitHub and never answers UNKNOWN,
so it is the terminal answer for whatever that poll budget does not resolve in
time. It clones for itself, so a per-PR event settles here exactly as a scan
does, rather than leaving the PR unlabelled until a later scan reaches it.

Reads `number<TAB>baseRefName` rows on stdin, clones the repo once (bare,
blobless, and unauthenticated unless the caller's environment carries a git
auth header), fetches every PR head and base ref the rows name, and prints
`number<TAB>MERGEABLE|CONFLICTING` per row on stdout. A row whose refs or
merge do not resolve is omitted, with a note on stderr.

A row may carry a third field, a commit id to merge INSTEAD of the base
branch's tip — auto-resolve's `base-sha` input. Such a row answers
BASE_SHA_MALFORMED or BASE_SHA_UNREACHABLE when that commit fails the
invariant `_pinned_base_problem` states, and a merge verdict otherwise.

Env: RUNNER_TEMP (optional) — where the scratch clone lives; falls back to the
system temp directory. Argv: --clone-url (required) — the repo to clone,
parameterised so a test can point it at a local bare repo.
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


# A full commit id, as GitHub spells one. A short or mixed-case id is refused
# rather than resolved: a prefix can name a different object tomorrow.
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True, kw_only=True, slots=True)
class Row:  # allow-duplicate-class: unrelated to other scanned Row types
    number: str
    base_ref: str
    # The commit merged in place of `base_ref`'s tip, or empty for the tip.
    base_sha: str = ""


def _read_rows(lines: list[str]) -> list[Row]:
    rows = []
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if not line:
            continue
        number, base_ref, *pinned = line.split("\t")
        if len(pinned) > 1:
            raise SystemExit(f"merge-conflict-probe: too many fields in row {line!r}")
        rows.append(Row(number=number, base_ref=base_ref, base_sha="".join(pinned)))
    return rows


def _run(*command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def _capture(*command: str, cwd: Path) -> str:
    """`command`'s stdout, or a SystemExit naming the command, its exit status
    and its own stderr. Not `check=True`, whose CalledProcessError message
    discards the subprocess's stderr."""
    res = _run(*command, cwd=cwd)
    if res.returncode != 0:
        raise SystemExit(
            f"{shlex.join(command)} failed (exit {res.returncode}): "
            f"{res.stderr.strip() or '<no stderr>'}"
        )
    return res.stdout


def _refspecs(rows: list[Row]) -> list[str]:
    """One fetch refspec per PR head, one per distinct base branch — a base
    push scan routinely lists several PRs sharing `main`, so the base set
    deduplicates rather than re-fetching the same branch once per PR."""
    heads = {f"refs/pull/{row.number}/head:refs/pull/{row.number}/head" for row in rows}
    bases = {f"refs/heads/{row.base_ref}:refs/heads/{row.base_ref}" for row in rows}
    return sorted(heads | bases)


def _fetch_all(repo: Path, rows: list[Row]) -> bool:
    """One fetch covering every row's refs, deduplicated. True on success; a
    single unfetchable ref (a base branch the origin no longer has, a head
    that moved) fails this whole call, so the caller falls to `_fetch_row`
    per row rather than losing every other row's verdict to one bad ref."""
    return _run("git", "fetch", "origin", *_refspecs(rows), cwd=repo).returncode == 0


def _fetch_row(repo: Path, row: Row) -> bool:
    """True on success; on failure, prints a stderr line naming the row and
    returns False so `main` can omit it — the same contract as `_verdict`
    below, so one bad row delays only itself, never the batch."""
    refspecs = sorted(
        {
            f"refs/pull/{row.number}/head:refs/pull/{row.number}/head",
            f"refs/heads/{row.base_ref}:refs/heads/{row.base_ref}",
        }
    )
    res = _run("git", "fetch", "origin", *refspecs, cwd=repo)
    if res.returncode != 0:
        print(
            f"merge-conflict-probe: PR {row.number}: fetch failed (exit "
            f"{res.returncode}): {res.stderr.strip() or '<no stderr>'}",
            file=sys.stderr,
        )
    return res.returncode == 0


def _base_is_gone(repo: Path, row: Row) -> bool:
    """True when the origin has no branch named by `row.base_ref`. A fetch
    failure alone cannot say this: a transient network error and a base branch
    someone deleted both fail the same way, and only the second is terminal.
    `git ls-remote --exit-code` answers 2 for no matching ref."""
    return (
        _run(
            "git",
            "ls-remote",
            "--exit-code",
            "--heads",
            "origin",
            f"refs/heads/{row.base_ref}",
            cwd=repo,
        ).returncode
        == 2
    )


def _pinned_base_problem(repo: Path, row: Row) -> str | None:
    """Why `row.base_sha` may not be merged, or None when it may.

    INVARIANT — a pinned base-side commit is a full commit id that a BRANCH of
    the cloned repository reaches. This refusal is what keeps a fork-only commit
    out: GitHub serves any object in a fork network by its id, and `refs/pull/*`
    carries fork heads, but only a push to the repository itself adds a branch.
    The id is checked before it reaches any git argv, so it cannot be an option."""
    if not _FULL_SHA.fullmatch(row.base_sha):
        return "BASE_SHA_MALFORMED"
    # A missing object makes `--contains` exit non-zero; that is unreachable too.
    reached = _run(
        "git",
        "for-each-ref",
        "--count=1",
        "--contains",
        row.base_sha,
        "refs/heads/",
        cwd=repo,
    )
    if reached.returncode != 0 or not reached.stdout.strip():
        return "BASE_SHA_UNREACHABLE"
    return None


def _verdict(repo: Path, row: Row) -> str | None:
    """MERGEABLE or CONFLICTING for `row`, from git's own merge of the fetched
    base and head refs. Exit 0 is a clean merge, exit 1 is git's own
    conflicted-but-written verdict; anything else means the refs this row
    named did not resolve to a real three-way merge at all, and believing a
    verdict from that would be worse than reporting one — so this prints the
    failure to stderr and returns None, letting the caller omit the row
    instead of discarding every other row's verdict with it."""
    if row.base_sha and (problem := _pinned_base_problem(repo, row)):
        return problem
    base_side = row.base_sha or f"refs/heads/{row.base_ref}"
    res = _run(
        "git",
        "merge-tree",
        "--write-tree",
        base_side,
        f"refs/pull/{row.number}/head",
        cwd=repo,
    )
    if res.returncode == 0:
        return "MERGEABLE"
    if res.returncode == 1:
        return "CONFLICTING"
    print(
        f"merge-conflict-probe: PR {row.number}: git merge-tree --write-tree "
        f"{base_side} refs/pull/{row.number}/head failed (exit "
        f"{res.returncode}): {res.stderr.strip() or '<no stderr>'}",
        file=sys.stderr,
    )
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute merge-conflict verdicts locally via git merge-tree."
    )
    parser.add_argument("--clone-url", required=True)
    args = parser.parse_args()
    rows = _read_rows(sys.stdin.readlines())
    if not rows:
        return
    with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP")) as scratch:
        repo = Path(scratch) / "probe.git"
        _capture(
            "git",
            "clone",
            "--bare",
            "--no-tags",
            "--filter=blob:none",
            args.clone_url,
            str(repo),
            cwd=Path(scratch),
        )
        if _fetch_all(repo, rows):
            settled_rows, gone_rows = rows, []
        else:
            fetched = [r for r in rows if _fetch_row(repo, r)]
            unfetched = [r for r in rows if r not in fetched]
            gone_rows = [r for r in unfetched if _base_is_gone(repo, r)]
            settled_rows = fetched
        # A base branch the origin no longer has is terminal, not transient: no
        # later scan can fetch it, and reporting CONFLICTING would send the
        # resolver to merge a ref it cannot resolve either. Say so distinctly.
        for row in gone_rows:
            print(f"{row.number}\tBASE_GONE")
        for row in settled_rows:
            verdict = _verdict(repo, row)
            if verdict is not None:
                print(f"{row.number}\t{verdict}")


if __name__ == "__main__":
    main()
