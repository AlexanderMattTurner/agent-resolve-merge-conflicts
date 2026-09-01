"""Where a conflicted file's body went, when one side left a stub behind.

PROBLEM CLASS — a merge conflict whose correct resolution edits a file that
did not conflict. One side MOVES a file's body to a new path and leaves a
small launcher at the old one; the other side keeps editing the old path. Git
rename detection cannot fire, because the old path still holds a file, so the
merge is not "rename plus edit" but "whole body replaced" against "whole body
edited" — one conflict hunk spanning the file. agent-glovebox #5289 hit this:
`sbx-kit/image/lib/egress_filter.py` went from 1523 lines to a 14-line
launcher while the base branch edited the old path, and the shard left the
markers in because neither side's text is right on its own.

The shard cannot fix that alone: taking the stub is the only coherent answer
inside its own file, and it silently discards the other side's work. So this
names the destination, and `prompts.relocation_notice` tells the shard to take
the stub and DECLINE with the destination named, which reaches a human with
the port spelled out instead of a wall of markers.

Detection is deliberately narrow, because a false positive tells a shard to
throw away a side. A candidate must be a path that side ADDED, carrying the
same basename, holding most of the base file's own distinctive lines.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _conflict_history import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    run_git,
)

# A stub is small in ABSOLUTE terms and small RELATIVE to what it replaced, so
# neither a short file nor a large trim alone reads as a relocation.
_STUB_MAX_BYTES = 4000
_STUB_MAX_FRACTION = 0.15
# The base must be big enough that "most of its lines" means something.
_BASE_MIN_LINES = 40
# Lines short enough to recur by chance (imports, `else:`, a brace) carry no
# evidence, so only longer ones are sampled.
_DISTINCTIVE_MIN_CHARS = 24
_SAMPLE_MAX_LINES = 120
_MATCH_MIN_FRACTION = 0.6

# The two merge stages, and the ref whose added paths each one may relocate to.
_SIDES = (("2", "HEAD", "this PR"), ("3", "MERGE_HEAD", "the base branch"))


@dataclass(frozen=True)
class Relocation:
    """One conflicted path whose body moved, and where it went."""

    path: str
    destination: str
    # Which side left the stub, in words a prompt can use.
    stub_side: str
    # The side whose edits therefore have no home in the conflicted file.
    stranded_side: str


def _blob(stage: str, path: str) -> str | None:
    done = run_git("show", f":{stage}:{path}")
    return done.stdout if done.returncode == 0 else None


def _added_paths(merge_base: str, ref: str) -> list[str]:
    done = run_git("diff", "--name-only", "--diff-filter=A", merge_base, ref)
    return done.stdout.split("\n") if done.returncode == 0 else []


def _distinctive(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for line in text.split("\n"):
        stripped = line.strip()
        if len(stripped) >= _DISTINCTIVE_MIN_CHARS:
            seen.setdefault(stripped, None)
    return list(seen)[:_SAMPLE_MAX_LINES]


def _carries(candidate: str, sample: list[str]) -> bool:
    """Whether CANDIDATE holds most of the sampled lines.

    Membership, not order: the move may reindent, reorder or drop a region and
    still be the same body at a new path.
    """
    if not sample:
        return False
    body = {line.strip() for line in candidate.split("\n")}
    hits = sum(1 for line in sample if line in body)
    return hits >= _MATCH_MIN_FRACTION * len(sample)


def _destination_for(
    stub: str, base: str, merge_base: str, ref: str, path: str
) -> str | None:
    """The added path on REF that carries BASE's body, or None."""
    if len(stub) > _STUB_MAX_BYTES or len(stub) > _STUB_MAX_FRACTION * len(base):
        return None
    if base.count("\n") < _BASE_MIN_LINES:
        return None
    sample = _distinctive(base)
    basename = Path(path).name
    for candidate in _added_paths(merge_base, ref):
        if not candidate or Path(candidate).name != basename:
            continue
        blob = run_git("show", f"{ref}:{candidate}")
        if blob.returncode == 0 and _carries(blob.stdout, sample):
            return candidate
    return None


def relocation_for(path: str) -> Relocation | None:
    """Where PATH's body went, when one side of this merge left a stub.

    Read from the mid-merge tree: stage 2 is the PR side (HEAD), stage 3 the
    base side (MERGE_HEAD). Returns None for every shape that is not this one,
    including anything git could not answer — a detector that guessed here
    would tell a shard to discard a side.
    """
    base = _blob("1", path)
    if not base:
        return None
    merge_base = run_git("merge-base", "HEAD", "MERGE_HEAD")
    if merge_base.returncode != 0:
        return None
    for stage, ref, side in _SIDES:
        stub = _blob(stage, path)
        if stub is None:
            continue
        destination = _destination_for(stub, base, merge_base.stdout.strip(), ref, path)
        if destination is not None:
            stranded = next(other for _, _, other in _SIDES if other != side)
            return Relocation(path, destination, side, stranded)
    return None


def relocations(paths: list[str]) -> dict[str, Relocation]:
    """The subset of PATHS whose body moved, keyed by path. Best-effort: a git
    call this run cannot make drops that path rather than failing the run."""
    found: dict[str, Relocation] = {}
    for path in paths:
        try:
            hit = relocation_for(path)
        except OSError as failure:
            print(
                f"::warning::could not test {path} for a relocation: {failure}",
                file=sys.stderr,
            )
            continue
        if hit is not None:
            found[path] = hit
    return found
