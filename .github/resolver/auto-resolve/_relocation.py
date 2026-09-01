"""Where a conflicted file's body went, when one side left a stub behind.

PROBLEM CLASS — a merge conflict whose answer is not inside the conflicted
file. One side MOVES a file's body to a new path and leaves a small launcher at
the old one; the other side keeps editing the old path. Git rename detection
cannot fire, because the old path still holds a file, so the merge is not
"rename plus edit" but "whole body replaced" against "whole body edited" and
git marks the whole file. A shard asked to merge those two texts has no correct
answer: the launcher is the right content, and writing it discards edits that
now belong at a path the shard may not touch.

So this names the destination and `prompts.relocation_notice` turns the shard's
job into a DECLINE that names it. The markers stay, because they are what
publishes a decline: `bundle.py` and `_marker_verdict.py` both read the decline
records of the files that still hold markers, so a marker-free file with a
decline record is dropped by every consumer and the run lands green having lost
one side's work.

Detection is deliberately narrow, because a false positive tells a shard a side
is redundant. A candidate must be a path that side ADDED, carrying the same
basename, holding most of the base file's own distinctive lines.
"""

import re
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
# The base must be long enough that "most of its lines" means something.
_BASE_MIN_LINES = 40
# Lines short enough to recur by chance (an import, `else:`, a brace) carry no
# evidence, so only longer ones are sampled.
_DISTINCTIVE_MIN_CHARS = 24
_SAMPLE_MAX_LINES = 120
_MATCH_MIN_FRACTION = 0.6

# The two merge stages, and the ref whose added paths each may relocate to.
_OURS = (":2", "HEAD", "this PR")
_THEIRS = (":3", "MERGE_HEAD", "the base branch")


@dataclass(frozen=True)
class Relocation:
    """One conflicted path whose body moved, and where it went.

    The stage and ref of each side travel WITH the relocation. They are known
    when it is built, and a consumer that re-derives them from the display words
    turns a reworded prompt string into a wrong merge stage.
    """

    path: str
    destination: str
    # Which side left the stub, in words a prompt can use.
    stub_side: str
    # The side whose edits therefore have no home in the conflicted file.
    stranded_side: str
    # That same pair as git names them: the index stage and the commit ref.
    stub_stage: str
    stub_ref: str
    stranded_stage: str
    stranded_ref: str


def _blob(ref_or_stage: str, path: str) -> str | None:
    """PATH's content at a merge STAGE (":1") or at a REF ("HEAD"), or None when
    git could not read it.

    A stage this merge has no entry for and a path a ref does not carry are both
    "no content", and neither is an error: an add/add conflict has no stage 1. A
    stage is spelled with its leading colon, because `git show 1:<path>` reads
    `1` as a revision and fails where `:1:<path>` reads the index.
    """
    done = run_git("show", f"{ref_or_stage}:{path}")
    return done.stdout if done.returncode == 0 else None


def _added_paths(merge_base: str, ref: str) -> list[str]:
    """Paths REF added since MERGE_BASE.

    -z, because git QUOTES a path holding a non-ASCII byte, a quote or a newline
    in its default output, and a quoted path matches no basename and reads back
    as no file — a silent miss on exactly the destinations hardest to notice.
    """
    done = run_git("diff", "-z", "--name-only", "--diff-filter=A", merge_base, ref)
    if done.returncode != 0:
        return []
    return [name for name in done.stdout.split("\0") if name]


def _distinctive(text: str) -> list[str]:
    """A sample of TEXT's own longer lines, spread across the whole file.

    Strided rather than truncated: a header of licence, imports and a module
    docstring is what two files SPLIT out of one still share, so a sample taken
    from the top alone would read either half as the destination.
    """
    lines = [
        stripped
        for stripped in (line.strip() for line in text.split("\n"))
        if len(stripped) >= _DISTINCTIVE_MIN_CHARS
    ]
    unique = list(dict.fromkeys(lines))
    if len(unique) <= _SAMPLE_MAX_LINES:
        return unique
    stride = len(unique) / _SAMPLE_MAX_LINES
    return [unique[int(index * stride)] for index in range(_SAMPLE_MAX_LINES)]


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


@dataclass(frozen=True)
class _MergeFacts:
    """What every path in one run is tested against, read once."""

    merge_base: str
    added: dict[str, list[str]]
    conflicted: frozenset[str]


def _merge_facts(conflicted: list[str]) -> _MergeFacts | None:
    base = run_git("merge-base", "HEAD", "MERGE_HEAD")
    if base.returncode != 0:
        return None
    merge_base = base.stdout.strip()
    return _MergeFacts(
        merge_base=merge_base,
        added={ref: _added_paths(merge_base, ref) for _, ref, _ in (_OURS, _THEIRS)},
        conflicted=frozenset(conflicted),
    )


def _stub_points_at(stub: str, destination: str) -> bool:
    """Whether STUB names where it now points.

    A launcher says where the body went — `from egress_gateway.egress_filter
    import main`. Content similarity alone cannot tell that from an archival
    copy the stub never references, and the port acts on this answer, so the
    stub must name a directory of the destination. The BASENAME is no evidence:
    a candidate only reaches here by sharing it.
    """
    words = set(re.findall(r"[A-Za-z0-9_-]+", stub))
    return any(part in words for part in Path(destination).parts[:-1])


def _destination_for(
    stub: str, base: str, facts: _MergeFacts, ref: str, path: str
) -> str | None:
    """The added path on REF that carries BASE's body, or None."""
    if len(stub) > _STUB_MAX_BYTES or len(stub) > _STUB_MAX_FRACTION * len(base):
        return None
    if base.count("\n") < _BASE_MIN_LINES:
        return None
    sample = _distinctive(base)
    basename = Path(path).name
    hits = []
    for candidate in facts.added[ref]:
        # A destination that is ITSELF conflicted belongs to another shard, and
        # telling this one to send its decline there names a moving target.
        if Path(candidate).name != basename or candidate in facts.conflicted:
            continue
        blob = _blob(ref, candidate)
        if (
            blob is not None
            and _carries(blob, sample)
            and _stub_points_at(stub, candidate)
        ):
            hits.append(candidate)
    # Two candidates carrying the same body — the real destination and an
    # archival copy — give no evidence which one the launcher points at, and
    # naming the wrong one sends the port into a file nobody reads.
    return hits[0] if len(hits) == 1 else None


def relocation_for(path: str, facts: _MergeFacts) -> Relocation | None:
    """Where PATH's body went, when one side of this merge left a stub.

    Read from the mid-merge tree: stage 2 is the PR side (HEAD), stage 3 the
    base side (MERGE_HEAD). Returns None for every shape that is not this one.
    """
    base = _blob(":1", path)
    if base is None:
        return None
    found = []
    for mover, stranded in ((_OURS, _THEIRS), (_THEIRS, _OURS)):
        stub_stage, mover_ref, stub_side = mover
        stranded_stage, stranded_ref, stranded_side = stranded
        stub = _blob(stub_stage, path)
        if stub is None:
            continue
        destination = _destination_for(stub, base, facts, mover_ref, path)
        if destination is not None:
            found.append(
                Relocation(
                    path=path,
                    destination=destination,
                    stub_side=stub_side,
                    stranded_side=stranded_side,
                    stub_stage=stub_stage,
                    stub_ref=mover_ref,
                    stranded_stage=stranded_stage,
                    stranded_ref=stranded_ref,
                )
            )
    # BOTH sides moving the body is a different conflict: neither side kept
    # editing the old path, so there is no stranded side to name and the notice
    # would tell one side its own relocation is the one to discard.
    return found[0] if len(found) == 1 else None


def relocations(paths: list[str], skip: set[str]) -> dict[str, Relocation]:
    """The subset of PATHS whose body moved, keyed by path.

    SKIP names the paths this cannot judge, and the caller cannot act on: a
    modify/delete conflict has no stages to compare, and a sidecar path is
    prompted by `sidecar_prompt`, which carries no relocation notice.

    Best-effort by contract: this only enriches a prompt, so anything it cannot
    read drops that path rather than failing the run. Non-UTF-8 bytes are the
    case that bites, because `run_git` decodes strictly. Both reads are guarded,
    not just the per-path one: `_added_paths` asks for RAW path bytes, so a merge
    that added a file whose NAME is not UTF-8 would otherwise end the fan-out
    before a shard starts, over a file no conflict names.
    """
    eligible = [path for path in paths if path not in skip]
    try:
        facts = _merge_facts(eligible)
    except (OSError, UnicodeDecodeError) as failure:
        print(
            f"::warning::could not read this merge for relocations: {failure}",
            file=sys.stderr,
        )
        return {}
    if facts is None:
        return {}
    found: dict[str, Relocation] = {}
    for path in eligible:
        try:
            hit = relocation_for(path, facts)
        except (OSError, UnicodeDecodeError) as failure:
            print(
                f"::warning::could not test {path} for a relocation: {failure}",
                file=sys.stderr,
            )
            continue
        if hit is not None:
            found[path] = hit
    return found
