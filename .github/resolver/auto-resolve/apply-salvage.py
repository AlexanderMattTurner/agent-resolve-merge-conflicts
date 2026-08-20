#!/usr/bin/env python3
"""Install a prior run's partial resolution into THIS run's merge.

PROBLEM CLASS — a conflict set larger than one run's wall clock never finishes,
because every run starts from the same untouched merge. The fan-out reaches
about `MAX_PARALLEL x (FANOUT_BUDGET_SECONDS / SHARD_TIMEOUT_SECONDS)` shards,
so a set past that size loses the same tail every time and pays the full price
to stop in the same place. Carrying what the last run DID resolve is what turns
that fixed ceiling into a set that shrinks: round N installs round N-1's paths
and spends its whole window on the remainder.

The install is deliberately dumb, because the alternative is a merge this
program is not entitled to make. `salvage.patch` is `git diff <merge base> --
<paths>`, so each path is restored to that exact merge base and the patch is
applied there — context matches by construction, or the round carries nothing.

Two pins decide whether the salvage may be installed at all, and both refuse
rather than guess: the recorded head must be the head this run checked out, and
the recorded merge base must be the one this run's merge used. A patch cut from
another base applies to text neither side wrote.

INVARIANT — a refusal leaves the merge byte-for-byte as git wrote it. The
conflict stages are saved before the first write and restored on any failure,
so a carry that does not take costs the run nothing but a log line.

Env: SALVAGE_DIR, HEAD_SHA, MERGE_BASE. Optional: GITHUB_OUTPUT.
"""

import json
import os
import subprocess
from pathlib import Path


def git(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, input=stdin
    )


def stand_down(reason: str) -> None:
    """Say why nothing was carried, and leave the merge untouched."""
    print(f"::notice::auto-resolve carried no prior resolution: {reason}")
    raise SystemExit(0)


def read_manifest(salvage_dir: Path) -> dict:
    try:
        document = json.loads(
            (salvage_dir / "salvage.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as err:
        stand_down(f"its manifest could not be read ({err})")
    if not isinstance(document, dict):
        stand_down("its manifest is not an object")
    return document


def install(salvage_dir: Path, paths: list[str], merge_base: str) -> None:
    """Restore PATHS to MERGE_BASE, apply the patch there, and stage the result.

    `git ls-files -u` prints exactly the lines `git update-index --index-info`
    reads back, so the saved stages ARE the undo for the index. The worktree's
    own bytes are saved beside them rather than regenerated: `git checkout -m`
    rewrites the conflict with `ours`/`theirs` labels where git had written
    `HEAD`/<branch>, and the shard prompts show a human those labels."""
    stages = git("ls-files", "-u", "--", *paths).stdout
    conflicted = {
        path: Path(path).read_bytes() for path in paths if Path(path).is_file()
    }

    def put_the_conflict_back(reason: str) -> None:
        git("update-index", "--index-info", stdin=stages)
        for path, text in conflicted.items():
            Path(path).write_bytes(text)
        stand_down(reason)

    restored = git("checkout", merge_base, "--", *paths)
    if restored.returncode:
        put_the_conflict_back(
            f"the merge base does not hold every carried path "
            f"({restored.stderr.strip()})"
        )
    applied = git("apply", str(salvage_dir / "salvage.patch"))
    if applied.returncode:
        put_the_conflict_back(
            f"its patch did not apply to this merge ({applied.stderr.strip()})"
        )
    staged = git("add", "--", *paths)
    if staged.returncode:
        put_the_conflict_back(
            f"the carried content could not be staged ({staged.stderr.strip()})"
        )


def main() -> None:
    if not os.environ.get("SALVAGE_DIR"):
        return
    salvage_dir = Path(os.environ["SALVAGE_DIR"])
    document = read_manifest(salvage_dir)
    head = os.environ.get("HEAD_SHA", "")
    if document.get("head") != head:
        stand_down(
            f"it resolved head {document.get('head')}, and this run is on {head}"
        )
    merge_base = os.environ.get("MERGE_BASE", "")
    if document.get("merge_base") != merge_base:
        stand_down(
            f"it was cut from merge base {document.get('merge_base')}, and this "
            f"merge used {merge_base}"
        )
    paths = document.get("paths")
    # An EMPTY list is the one that bites: `all()` is vacuously true for it, and
    # `git checkout <base> --` with no pathspec then takes the WHOLE tree, which
    # is every conflict in the merge rather than the ones a round resolved.
    if (
        not isinstance(paths, list)
        or not paths
        or not all(isinstance(path, str) and path for path in paths)
    ):
        stand_down("its manifest names no usable paths")
    install(salvage_dir, paths, merge_base)
    print(
        f"carried {len(paths)} path(s) that round {document.get('round')} of this "
        f"head's resolution already resolved: {', '.join(paths)}. This run's "
        "window buys only what is still conflicted."
    )
    if output := os.environ.get("GITHUB_OUTPUT", ""):
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"carried={len(paths)}\n")


if __name__ == "__main__":
    main()
