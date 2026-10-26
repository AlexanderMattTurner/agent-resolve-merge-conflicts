"""Keep the merge a refused run reached, so a hand resolution starts from it.

A refusal ends the run, and the runner takes its merged tree with it. This writes
that tree, conflict markers and all, as one merge commit in `attempt.bundle` with
`attempt.json` beside it, in BUNDLE_DIR. The workflow uploads that directory as the
run's `auto-resolve-merge-<pr>` artifact. `land` and `reuse-bundle.py` read only
`merge.bundle`, so an attempt is never pushed and never reused as a resolution.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from _conflict_hunks import introduced_marker_paths
from _git_io import bound_repo, git, git_status

ATTEMPT_REF = "refs/auto-resolve/attempt"


def _parents() -> list[str]:
    """The merge's two parents, by the same rule as bundle.py's `read_parents`;
    empty when the checkout holds no merge at all."""
    if git_status("rev-parse", "-q", "--verify", "MERGE_HEAD") == 0:
        return [
            git("rev-parse", "HEAD").strip(),
            git("rev-parse", "MERGE_HEAD").strip(),
        ]
    if git_status("rev-parse", "-q", "--verify", "HEAD^2") == 0:
        return [git("rev-parse", "HEAD^1").strip(), git("rev-parse", "HEAD^2").strip()]
    return []


def _commit_the_worktree(parents: list[str]) -> str:
    """A merge commit of every tracked file as the worktree holds it now, built in a
    scratch copy of the index. An unmerged path is taken with its markers, so the
    commit holds exactly what a person would open."""
    with tempfile.TemporaryDirectory() as scratch:
        index = Path(scratch) / "index"
        shutil.copyfile(
            git("rev-parse", "--path-format=absolute", "--git-path", "index").strip(),
            index,
        )
        git("add", "--update", "--", ":/", index=index)
        tree = git("write-tree", index=index).strip()
    parent_args = [arg for sha in parents for arg in ("-p", sha)]
    return git(
        "-c",
        "user.name=auto-resolve",
        "-c",
        "user.email=auto-resolve@users.noreply.github.com",
        "commit-tree",
        tree,
        *parent_args,
        "-m",
        "auto-resolve: the merge a refused run reached",
    ).strip()


def keep_the_attempt() -> str:
    """Write the attempt and return the paragraph the refusal comment carries, or
    an empty string when the run reached no merge to keep."""
    bundle_dir = os.environ.get("BUNDLE_DIR", "")
    parents = _parents() if bundle_dir else []
    if not parents:
        return ""
    commit = _commit_the_worktree(parents)
    out = Path(bundle_dir)
    out.mkdir(parents=True, exist_ok=True)
    git("update-ref", ATTEMPT_REF, commit)
    git(
        "bundle",
        "create",
        str(out / "attempt.bundle"),
        ATTEMPT_REF,
        *(f"^{p}" for p in parents),
    )
    marked = introduced_marker_paths(bound_repo(), parents, ":/", tree=commit)
    (out / "attempt.json").write_text(
        json.dumps(
            {
                "commit": commit,
                "head": parents[0],
                "base": parents[1],
                "still_conflicted": marked,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    pr = os.environ.get("PR", "")
    left = (
        f" {len(marked)} file(s) in it still carry conflict markers." if marked else ""
    )
    return (
        f"**The merge this run reached is kept**: a merge of `{parents[0][:12]}` with "
        f"`{parents[1][:12]}`, as `attempt.bundle` in this run's "
        f"`auto-resolve-merge-{pr}` artifact.{left} To resolve by hand from it, on the "
        "pull request's branch at that head:\n\n"
        "```sh\n"
        f"gh run download {os.environ.get('GITHUB_RUN_ID', '<run-id>')} "
        f"-R {os.environ.get('GH_REPO', '<owner/repo>')} -n auto-resolve-merge-{pr} -D /tmp/auto-resolve-{pr}\n"
        f"git fetch origin {parents[1]}\n"
        f"git fetch /tmp/auto-resolve-{pr}/attempt.bundle {ATTEMPT_REF}\n"
        "git merge --no-commit FETCH_HEAD^2\n"
        "git restore --source=FETCH_HEAD --staged --worktree -- :/\n"
        "```"
    )
