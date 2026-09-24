#!/usr/bin/env python3
"""Auto-resolve merge conflicts — the GENERATED-REGION pre-pass.

PROBLEM CLASS — a conflict inside a `BEGIN GENERATED`/`END GENERATED` region is
DERIVED content, so neither side was authored and neither side is the answer.
Handing one to the model pays for a judgement nobody has to make, and on a
region that is one 4,000-character line it does not make it: run 5503 spent
$0.54 on `bash-mutation.yaml`'s `paths-regex` region, leaving its markers.

The generator that owns a region is named on the region's own BEGIN line, so
this step carries no table of its own. A file whose every hunk sits inside such
a region takes OURS, runs the generators the regions name, and must come back
marker-free. A side taken only produces a file the generator can then overwrite.

A region one parent DELETED whole is the other shape. Git drops its markers
cleanly, so the conflict holds one hunk with an empty side and no marker around
it, and the removal — not the derived content — is the answer.

A file with one hunk OUTSIDE any generated region is left whole to the LLM.
Resolving the rest would hand the model a file whose remaining markers no longer
match the ones its prompt describes.

A file whose generator EXITS NON-ZERO is deferred to bundle.py, which runs this
pass again against the tree the LLM resolved. Every other failure falls through
to the LLM with a warning.
"""

import functools
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:  # the merge tree supplies this module, so it is import-time absent
    from lib_marked_region import MarkedRegion

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _conflict_hunks import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    OURS,
    THEIRS,
    Hunk,
    has_markers,
    segments,
    side_of,
    splice,
)
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    bind_repo,
    bound_repo,
    git,
    git_lines,
    git_result,
)
from _refusal import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    reap_group,
)


def scripts_dir(tree: Path) -> Path:
    """The `scripts/` directory holding the marker definition, given the MERGE TREE.

    The marker convention this pass reads is the one the generators WRITE, so it
    comes from their module rather than from a regex here. That module belongs to
    the repository being merged, NOT to this resolver: the reusable workflow
    checks the two out separately, so the path is derived from the merge tree
    rather than from this file's own ancestors. The refusal is what verifies the
    count: a caller that declared marked-region support and has no module gets an
    error naming the directory it looked in, not a silent `ImportError`.
    """
    found = tree / "scripts"
    if not (found / "lib_marked_region.py").is_file():
        raise RuntimeError(
            f"{found} holds no lib_marked_region.py, but AUTO_RESOLVE_MARKED_REGIONS "
            "is set — the marker definition this pass reads regions with is not "
            "reachable in the tree being merged."
        )
    return found


@functools.cache
def _marked_regions_reader():
    """Bind the merge tree's `marked_regions`, or None when the caller declared no
    marked-region support.

    Cached because `resolve_generated_regions` asks once per conflicted file, and
    an uncached call would push a duplicate entry onto `sys.path` each time.

    Imported lazily and not at module scope: the module lives in the tree being
    merged, which `main` binds only once it is running. A caller that never sets
    AUTO_RESOLVE_MARKED_REGIONS has no such regions, so every file falls through
    to the LLM exactly as it did before this pass existed.
    """
    if os.environ.get("AUTO_RESOLVE_MARKED_REGIONS") != "true":
        return None
    sys.path.insert(0, str(scripts_dir(bound_repo())))
    from lib_marked_region import (  # pylint: disable=import-error,import-outside-toplevel
        marked_regions,
    )

    return marked_regions


# Only a Python generator runs here. The `.mjs` ones reach their outputs through
# `pnpm resolve-generated`, which prepare.sh already ran, so a region naming one
# is a region that pass declined — repeating it would resolve nothing.
_RUNNABLE_SUFFIX = ".py"


def _holds(region: "MarkedRegion", start: int, stop: int) -> bool:
    """Whether the lines START..STOP sit strictly between REGION's markers."""
    return region.begin < start and stop < region.end


class HunkSpan(NamedTuple):
    """One conflict region and the 0-based lines it occupies, its marker lines
    included."""

    hunk: Hunk
    start: int
    stop: int


def _hunk_spans(text: str) -> list[HunkSpan] | None:
    """Each conflict region in TEXT with the 0-based lines it spans, or None
    when the markers do not parse into regions at all."""
    parts = segments(text)
    if parts is None:
        return None
    spans: list[HunkSpan] = []
    line = 0
    for part in parts:
        body = part if isinstance(part, str) else part.text
        length = len(body.splitlines())
        if isinstance(part, Hunk):
            spans.append(HunkSpan(part, line, line + length - 1))
        line += length
    return spans


class Plan(NamedTuple):
    """How to resolve one file: the text each conflict ordinal takes, and the
    generators that must run over the result."""

    sides: dict[int, str]
    generators: set[str]


#: The merge tree's `marked_regions`, as `_marked_regions_reader` hands it back.
MarkedRegions = Callable[[str], list["MarkedRegion"]]


def _sole_span(parent: str, block: str) -> tuple[int, int] | None:
    """The 0-based first and last line of BLOCK's ONE occurrence in PARENT.

    None when PARENT holds the block twice or not at all. Two occurrences say
    nothing about which region the block came out of, and none says the block is
    not a verbatim run of that parent's lines at all.
    """
    lines = parent.splitlines()
    wanted = block.splitlines()
    if not wanted:
        return None
    starts = [
        start
        for start in range(len(lines) - len(wanted) + 1)
        if lines[start : start + len(wanted)] == wanted
    ]
    if len(starts) != 1:
        return None
    return starts[0], starts[0] + len(wanted) - 1


def _parent_text(stage: str, path: str) -> str:
    """PATH's whole file at merge STAGE, read from the BOUND repository, or ""
    when git cannot hand it back as text.

    The read goes through `_git_io`, so it answers about the repository this pass
    was bound to rather than the directory the process happens to sit in. A stage
    this merge holds no entry for, and a blob holding a byte that is not UTF-8,
    are both "no answer": `bundle.py` calls this pass IN PROCESS, so a raise here
    ends the step with a traceback after the model has already been billed.
    """
    try:
        done = git_result("show", f"{stage}:{path}")
    except UnicodeDecodeError:
        return ""
    return done.stdout if done.returncode == 0 else ""


def _removed_region(hunk: Hunk, path: str, reader: MarkedRegions) -> bool:
    """Whether HUNK is one parent DELETING a marked region the other kept.

    One side is empty, and the other side's lines sit strictly inside a region
    of their own parent's WHOLE file. The parents are read for that, because the
    deletion took the markers with it and the conflicted text no longer carries
    them.

    The region's label decides it: a label no region of the other parent carries
    is a region that parent REMOVED. A parent that still carries the label moved
    or rewrote the region instead, so the removal is not the answer.

    Both parents must READ, because the label test is an absence: an unreadable
    remover carries no labels at all, so every label counts as removed and this
    would delete the kept parent's block on the strength of a failed git call.
    """
    ours, theirs = side_of(hunk.text, OURS), side_of(hunk.text, THEIRS)
    if (ours == "") == (theirs == ""):
        return False
    block, keeper, remover = (
        (ours, _parent_text(":2", path), _parent_text(":3", path))
        if theirs == ""
        else (theirs, _parent_text(":3", path), _parent_text(":2", path))
    )
    if not keeper or not remover:
        return False
    span = _sole_span(keeper, block)
    if span is None:
        return False
    kept_labels = {region.where for region in reader(remover)}
    return any(
        _holds(region, *span) and region.where not in kept_labels
        for region in reader(keeper)
    )


def plan_for(text: str, path: str) -> Plan | None:
    """How to resolve TEXT's conflicts at PATH, or None when this pass declines.

    A hunk strictly inside a marked region of TEXT is derived content: it takes
    OURS, and the region's generator overwrites that on the next line. A hunk
    that is one parent deleting a marked region takes the empty side, and needs
    no generator.

    None covers every reason to leave the file whole: markers that do not parse,
    a file with no conflict at all, a hunk that is neither shape, a region whose
    generator this step cannot run, and a caller that declared no marked-region
    support.
    """
    reader = _marked_regions_reader()
    if reader is None:
        return None
    spans = _hunk_spans(text)
    if not spans:
        return None
    found = reader(text)
    sides: dict[int, str] = {}
    generators: set[str] = set()
    removed = False
    for span in spans:
        holder = next((r for r in found if _holds(r, span.start, span.stop)), None)
        if holder is not None and holder.generator.endswith(_RUNNABLE_SUFFIX):
            sides[span.hunk.ordinal] = side_of(span.hunk.text, OURS)
            generators.add(holder.generator)
        elif _removed_region(span.hunk, path, reader):
            sides[span.hunk.ordinal] = ""
            removed = True
        else:
            return None
    # A deletion and a generator in ONE file go to the LLM. A generator writes
    # whatever its own markers delimit, and nothing here says it will not put back
    # the region the other hunk just removed — `has_markers` would then pass over a
    # block this pass decided nobody owns.
    if removed and generators:
        return None
    return Plan(sides, generators)


def take_ours(text: str) -> str:
    """TEXT with every conflict region replaced by its OURS side."""
    spans = _hunk_spans(text)
    if spans is None:
        raise ValueError("cannot resolve a file whose conflict markers do not parse")
    return splice(text, {s.hunk.ordinal: side_of(s.hunk.text, OURS) for s in spans})


# INVARIANT: a generator runs with these variables and NOTHING else, which is
# what stops it reading a model credential. The generator is a file the PR may
# have rewritten, and bundle.py calls this pass from the step that holds every
# `RUNG_*_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN*` and `FAR_ANTHROPIC_API_KEY`. An
# allowlist is what makes a credential added to that step later scrub itself;
# a deny list would carry the new name only once somebody remembered it. No
# generator in this tree reads the environment at all — these five are process
# hygiene, for the `git` and `grep` a generator subprocesses.
_GENERATOR_ENV_KEEP = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"})


def generator_env() -> dict[str, str]:
    """This process's environment, less everything not on the allowlist above."""
    return {k: v for k, v in os.environ.items() if k in _GENERATOR_ENV_KEEP}


def _run_generator(generator: str) -> bool:
    """Run GENERATOR against the merged tree, reporting whether it succeeded.

    THIS interpreter, never `uv run`: these generators import `tree_sitter_bash`
    through `_shell_scan`, plus `yaml` and `pathspec`, and `install-hook-tools.sh`
    already pip-installed that set here from the BASE ref's pins, before
    prepare.sh launched this pass. `uv run` would instead resolve the project
    whose root is `cwd` — the PR head left mid-merge — so the merged
    `pyproject.toml` would choose what a job holding the write token installs and
    builds, which is the posture every neighbouring step is written to keep.
    `_stand_in_for_generators` takes a side at every conflicted path whose
    markers parse, so a marker is still standing here only when some path's
    markers do not parse at all — and failing on that one is the honest
    outcome, since the region is not derivable from a tree that does not parse.
    """
    with subprocess.Popen(  # noqa: S603
        [sys.executable, generator],
        cwd=bound_repo(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=generator_env(),
        start_new_session=True,
    ) as proc:
        _out, err = proc.communicate()
    # A generator that leaves a child running keeps writing this tree, and the
    # `git add` below is the next thing to take the index. Reaped OUTSIDE the
    # `with`, so the generator's own zombie is not read as a live group member.
    reap_group(proc.pid)
    if proc.returncode == 0:
        return True
    sys.stderr.write(err)
    print(
        f"::warning::the generated-region pre-pass could not run {generator} "
        f"(exit {proc.returncode}); its regions are deferred — prepare.sh keeps "
        "them from the LLM, and bundle.py refuses the resolution."
    )
    return False


def unmerged_paths() -> list[str]:
    return git_lines("diff", "--name-only", "--diff-filter=U")


def _conflicted_text(path: Path) -> str | None:
    """PATH's conflicted text, or None when it holds no text this pass can read.

    Two members of the unmerged set are forgiven here, because each is a normal
    input to this reader rather than a failure. A binary conflict carries no
    markers and has its own partition in prepare.sh. An unmerged path with NO
    work-tree file has no region to derive either: git leaves the surviving side
    there for an ordinary modify/delete, so this is the path a generator deleted
    because the merge removed its source. Every other read error propagates: a
    file this pass cannot read for any other reason is a defect in this pass, and
    prepare.sh's warning is where it surfaces.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        return None


class RegionOutcome(NamedTuple):
    """What this pass did with the unmerged set: the paths it staged, and the
    paths whose generator could not run against a tree that is still
    conflicted."""

    staged: list[str]
    deferred: list[str]


def _stand_in_for_generators(root: Path, declined: dict[str, str]) -> list[str]:
    """Take OURS at every DECLINED path, so the generators can read the tree
    they walk. Returns the paths stood in for, in the order given.

    A generator derives its region from the WHOLE tree, so one sibling file
    still holding `<<<<<<<` ends the run and every candidate falls through to
    the LLM: a generator that reads its inputs with `ast.parse` raises
    `SyntaxError` on a marker line. Which side stands in decides nothing, because nothing here is staged and the
    caller's `finally` writes the conflicted bytes back — a stand-in lives only
    while a generator is reading. A path whose markers do not parse gets none:
    `take_ours` refuses it, and the generator then fails as it did before.
    """
    stood_in: list[str] = []
    for path, text in declined.items():
        if not _hunk_spans(text):
            continue
        (root / path).write_text(take_ours(text), encoding="utf-8")
        stood_in.append(path)
    return stood_in


def resolve_generated_regions(
    paths: list[str], *, llm_runs_next: bool
) -> RegionOutcome:
    """Resolve every PATH whose conflicts are all generated-region conflicts.

    LLM_RUNS_NEXT says whether the model still gets the paths this pass declined:
    true from prepare.sh, false from bundle.py, which runs after the model. It
    picks the stand-in warning's tail, and it takes no default because a wrong
    one would tell the reader which pass they are in and be wrong half the time.

    Every OTHER unmerged path ends holding the exact bytes git wrote, so it
    reaches the LLM with the markers its prompt describes. The snapshot covers
    the whole unmerged set rather than the candidates alone: a generator
    rewrites every output it owns, so a file this pass declined can still be
    rewritten by a candidate's generator, and prepare.sh's own restore loop
    skips unmerged paths by design. That same snapshot is what lets a declined
    path hold a marker-free stand-in while the generators run, and a warning
    names every region derived over one.

    A candidate whose generator EXITED NON-ZERO is deferred rather than
    declined. The common cause is another file in this same merge: a generator
    that walks the tree parses one of the conflicted files, and `<<<<<<<` is a
    syntax error in every language. The LLM does not merge these regions — a
    15,000-character derived line is what the region holds — so the file goes to
    bundle.py, which runs this pass again once the LLM has resolved the rest.
    """
    root = bound_repo()
    candidates: dict[str, Plan] = {}
    texts: dict[str, str] = {}
    declined: dict[str, str] = {}
    for path in paths:
        text = _conflicted_text(root / path)
        if text is None:
            continue
        plan = plan_for(text, path)
        if plan is None:
            declined[path] = text
            continue
        candidates[path] = plan
        texts[path] = text
    if not candidates:
        return RegionOutcome([], [])

    conflicted = {path: (root / path).read_bytes() for path in paths}
    for path, plan in candidates.items():
        (root / path).write_text(splice(texts[path], plan.sides), encoding="utf-8")
    stood_in = _stand_in_for_generators(root, declined)

    staged: list[str] = []
    derived: list[str] = []
    deferred: list[str] = []
    try:
        broken = {
            generator
            for generator in sorted(
                {g for plan in candidates.values() for g in plan.generators}
            )
            if not _run_generator(generator)
        }

        for path, plan in candidates.items():
            if plan.generators & broken:
                deferred.append(path)
                continue
            if has_markers((root / path).read_bytes()):
                print(
                    f"::warning::{path} still carries conflict markers after "
                    "its generator ran; leaving it to the LLM."
                )
                continue
            git("add", "--", path)
            staged.append(path)
            if plan.generators:
                derived.append(path)
        if derived and stood_in:
            tail = (
                "The LLM resolves those paths after this pass, and nothing "
                "re-derives the regions."
                if llm_runs_next
                else "The LLM has already run, so those paths now go to salvage "
                "or to the marker refusal."
            )
            print(
                f"::warning::the regions in {' '.join(derived)} were derived "
                f"from a tree holding OURS at {' '.join(stood_in)}, so a change "
                f"only THEIRS makes is missing from them. {tail}"
            )
    finally:
        # `finally`, not a call per failure arm: a missing `uv` raises
        # FileNotFoundError out of the generator run and `git` raises SystemExit,
        # and either would otherwise leave every candidate holding OURS with its
        # markers already stripped — unmerged, so nothing downstream puts it back.
        for path, blob in conflicted.items():
            if path not in staged:
                (root / path).write_bytes(blob)
    return RegionOutcome(staged, deferred)


def main() -> None:
    """Run the pass over the bound tree, naming the deferrals where the caller
    reads them: `REGION_DEFER_FILE`, one path per line, when it is set."""
    bind_repo(Path.cwd())
    outcome = resolve_generated_regions(unmerged_paths(), llm_runs_next=True)
    if outcome.staged:
        print(
            f"Re-derived {len(outcome.staged)} generated-region conflict(s) with "
            f"their own generator, skipping the LLM: {' '.join(outcome.staged)}"
        )
    if outcome.deferred:
        print(
            f"Deferring {len(outcome.deferred)} generated-region conflict(s) to "
            "post-LLM re-derivation, since their generator cannot read a tree "
            f"that is still conflicted: {' '.join(outcome.deferred)}"
        )
    defer_file = os.environ.get("REGION_DEFER_FILE")
    if defer_file:
        Path(defer_file).write_text(
            "".join(f"{path}\n" for path in outcome.deferred), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
