"""A read-only copy of what each side of the merge did, which every shard may search.

PROBLEM CLASS — a shard judging a conflict it cannot investigate. A shard has no shell, so
it cannot run `git show`, `git log` or read the pull request it is resolving. It sees one
file and the commit subjects its prompt carries, and a question those do not answer ends
in a decline (agent-glovebox#7124). This module answers those questions ahead of time, as
plain files the shard reaches with Read and Grep, so it can look up what it needs.

Layout, under the directory `write_context` returns:
- `README.md`: this layout, with the three commits it was read from.
- `pr.md`: the pull request's title and body.
- `merge-base/`, `pr-side/`, `base-side/`: each path either side changed, as that
  commit holds it. A path a side deleted is absent from that side's directory.
- `pr-side.log`, `base-side.log`: each side's commits since the merge base, with stats.
- `pr-side.diff`, `base-side.diff`: each side's whole change since the merge base.
- `decided.md`: keep-or-delete verdicts, once `run_in_waves` has made them.
- `.source`: the two commits this record was read from.

Only regular files are copied. A symlink in the copy would let a Read of a path under this
directory reach whatever the link names, and the read grant covers this directory by path.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _result_fields import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    read_verdict,
)

if TYPE_CHECKING:
    from fanout import Fanout

#: The two sides, named for the reader: HEAD is the pull request, MERGE_HEAD is the branch
#: it merges in.
SIDES = {"pr-side": "HEAD", "base-side": "MERGE_HEAD"}

#: Git's modes for a blob that is a regular file. Everything else — a symlink, a
#: submodule — stays out of the copy, per the module docstring.
_REGULAR_MODES = frozenset({"100644", "100755"})


def _git(*args: str, stdin: str | None = None, env: dict | None = None) -> str:
    """git ARGS in the working directory, raising on any failure."""
    # cwd-git-ok: the fan-out runs in the mid-merge checkout, and every call here READS
    #   it or writes outside it.
    return subprocess.run(
        ["git", *args],
        input=stdin,
        capture_output=True,
        text=True,
        # A diff or log quoting a file that is not UTF-8 still describes the merge.
        errors="replace",
        check=True,
        env=env,
    ).stdout


def _git_into(path: Path, *args: str) -> None:
    """git ARGS' output streamed into PATH, so a large diff is never held in memory."""
    with path.open("wb") as out:
        # cwd-git-ok: reads the mid-merge checkout, as `_git` does.
        subprocess.run(["git", *args], stdout=out, stderr=subprocess.PIPE, check=True)


class Change(NamedTuple):
    """One path a side changed, with its git mode at the merge base and at that side.
    A mode of `000000` means the path is absent at that commit."""

    path: str
    base_mode: str
    side_mode: str


def _changed(merge_base: str, ref: str) -> list[Change]:
    """Each path REF changed since MERGE_BASE. Renames are split into a delete and an
    add, so each path is named at the commit that holds it."""
    fields = _git("diff", "--raw", "-z", "--no-renames", merge_base, ref).split("\0")
    changed = []
    # `--raw -z` prints `:<old mode> <new mode> <old sha> <new sha> <status>` then the path.
    for meta, path in zip(fields[0::2], fields[1::2], strict=False):
        if not meta:
            continue
        old_mode, new_mode = meta.lstrip(":").split()[:2]
        changed.append(Change(path, old_mode, new_mode))
    return changed


def _copy_out(ref: str, paths: list[str], dest: Path) -> None:
    """Write PATHS as REF holds them under DEST, through a private index.

    `checkout-index` takes the path list on stdin, so a merge that changed many files
    costs one process rather than one `git show` each.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if not paths:
        return
    # Attributes from the EMPTY tree, so the branch's own `.gitattributes` cannot
    # re-encode the copy (`eol=crlf`, `working-tree-encoding`) away from the blob.
    no_attributes = (
        "--attr-source=" + _git("hash-object", "-t", "tree", "/dev/null").strip()
    )
    with tempfile.TemporaryDirectory() as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": f"{scratch}/index"}
        _git(no_attributes, "read-tree", ref, env=env)
        _git(
            no_attributes,
            "checkout-index",
            "-z",
            "--stdin",
            f"--prefix={dest}/",
            stdin="".join(f"{path}\0" for path in paths),
            env=env,
        )


def _pr_text(pr_number: str) -> str:
    """The pull request's title and body, or a line saying why they are missing."""
    repo = os.environ.get("GH_REPO", "")
    if not (repo and pr_number and shutil.which("gh")):
        return "The pull request's text was not available to this run.\n"
    done = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{pr_number}"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        pull = json.loads(done.stdout) if done.returncode == 0 else None
    except json.JSONDecodeError:
        pull = None
    if not isinstance(pull, dict):
        print(
            f"::warning::could not read PR #{pr_number}'s text for the resolver "
            f"(gh exited {done.returncode}); shards resolve without it.",
            file=sys.stderr,
        )
        return "The pull request's text was not available to this run.\n"
    return f"# {pull.get('title') or ''}\n\n{pull.get('body') or ''}\n"


def write_context(dest: Path, pr_number: str) -> Path | None:
    """Write the layout the module docstring gives under DEST.

    A record already there for the same two commits is kept, less its `decided.md`: each
    credential rung re-runs the fan-out on the same merge. None, loudly, outside a merge:
    MERGE_HEAD is what names the other side.
    """
    merging = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        capture_output=True,
        check=False,
    )
    if merging.returncode != 0:
        print(
            "::warning::no merge is in progress, so the shards run without the merge "
            "record.",
            file=sys.stderr,
        )
        return None
    heads = _git("rev-parse", "HEAD", "MERGE_HEAD")
    source = dest / ".source"
    if source.is_file() and source.read_text(encoding="utf-8") == heads:
        (dest / "decided.md").unlink(missing_ok=True)
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    merge_base = _git("merge-base", "HEAD", "MERGE_HEAD").strip()
    at_base: set[str] = set()
    for name, ref in SIDES.items():
        changed = _changed(merge_base, ref)
        _copy_out(
            ref, [p for p, _, new in changed if new in _REGULAR_MODES], dest / name
        )
        at_base.update(p for p, old, _ in changed if old in _REGULAR_MODES)
        _git_into(
            dest / f"{name}.log", "log", "--no-merges", "--stat", f"{merge_base}..{ref}"
        )
        _git_into(dest / f"{name}.diff", "diff", "--no-renames", merge_base, ref)
    _copy_out(merge_base, sorted(at_base), dest / "merge-base")
    (dest / "pr.md").write_text(_pr_text(pr_number), encoding="utf-8")
    (dest / "README.md").write_text(
        "What each side of this merge did, copied out of git before any shard ran.\n\n"
        f"- `merge-base/`: the common ancestor `git merge-base` names, {merge_base}.\n"
        f"- `pr-side/`: the pull request (HEAD, {_git('rev-parse', 'HEAD').strip()}).\n"
        "- `base-side/`: the branch merged into it "
        f"(MERGE_HEAD, {_git('rev-parse', 'MERGE_HEAD').strip()}).\n\n"
        "Each directory holds only the files some side changed since the merge base. A\n"
        "file missing from `pr-side/` or `base-side/` was deleted on that side. Each side's\n"
        "`.log` lists its commits, and its `.diff` is its whole change. `pr.md` is the pull\n"
        "request's own text. `decided.md`, when present, holds keep-or-delete answers\n"
        "already made for this merge.\n",
        encoding="utf-8",
    )
    # Last, so a run killed mid-write leaves no marker and the next rung rewrites it.
    source.write_text(heads, encoding="utf-8")
    return dest


#: How much of one shard's reasoning each later prompt carries. The whole of it is in the
#: verdict file; a prompt needs only enough to act on.
_REASON_CHARS = 300


def decided_text(verdicts: dict[str, dict | None]) -> str:
    """VERDICTS as prompt lines, one per path, empty when there are none.

    Each verdict is `{"decision": ..., "reasoning": ...}`, the shape `read_verdict` returns,
    or None for a shard that answered nothing. The reasoning is model text read from
    branch content, so it is folded onto one line and quoted: a newline in it would
    otherwise forge a verdict line for another path.
    """
    lines = []
    for path, verdict in sorted(verdicts.items()):
        decision = (verdict or {}).get("decision")
        outcome = {"keep": "keep", "delete": "delete"}.get(decision, "undecided")
        reason = " ".join(str((verdict or {}).get("reasoning") or "").split())
        note = f' (its shard said: "{reason[:_REASON_CHARS]}")' if reason else ""
        lines.append(f"- `{path}`: {outcome}{note}")
    return "".join(f"{line}\n" for line in lines)


def run_in_waves(fanout: "Fanout") -> None:
    """Run every shard of FANOUT: the keep-or-delete verdicts first, then the rest with
    those verdicts in their prompts.

    INVARIANT — a conflict that calls a file another conflict deletes is resolved knowing
    the answer. Run side by side, the shard holding the caller could only guess how the
    deletion would go, and the deleting shard could only decline (agent-glovebox#7124).

    Both waves share the fan-out's one deadline. A verdict is a short answer, so a
    first-wave shard gets at most a quarter of what is left, and one slow verdict
    cannot spend the clock the second wave needs.
    """
    indexed = list(enumerate(fanout.work))
    first = [pair for pair in indexed if pair[1].path in fanout.modify_delete]
    rest = [pair for pair in indexed if pair[1].path not in fanout.modify_delete]
    full_timeout = fanout.shard_timeout
    if first:
        fanout.shard_timeout = min(full_timeout, (fanout.deadline - monotonic()) / 4)
    for wave in (first, rest):
        if wave is rest:
            fanout.shard_timeout = full_timeout
            fanout.decided = decided_text(
                {
                    work.path: read_verdict(Path(fanout.verdict_path(index)))
                    for index, work in first
                }
            )
            if fanout.decided and fanout.context_dir is not None:
                (fanout.context_dir / "decided.md").write_text(
                    fanout.decided, encoding="utf-8"
                )
        # Bounded: the resolve runs against one shared LLM credential and an
        # account-wide runner pool.
        with ThreadPoolExecutor(max_workers=fanout.max_parallel) as pool:
            list(pool.map(lambda pair: fanout.shard_worker(*pair), wave))
