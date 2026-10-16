"""The fan-out's SECOND runs: one more whole-file run on a file whose first
answer the run cannot take, told why. Two first answers qualify:

* silence — every shard of the file reported success and the file still holds
  markers with no decline recorded. One block shard that answers nothing would
  otherwise cost the run every other shard's answer.
* a one-sided take — PROBLEM CLASS: a shard answers a conflicted file with ONE
  parent's exact bytes while the OTHER parent changed that file since the merge
  base. Every line traces to a parent, so nothing before the post-merge check
  (`_contradictory_merge`) sees it, and that check can only report it or spend
  its own budget on a repair. `_taken_whole` owns the predicate.

Each gets ONE second run, inside the fan-out's own deadline. `fanout.py` owns
how a run is launched and summarized; this module decides which files get one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _conflict_history import (  # pylint: disable=wrong-import-position
    run_git,
)
from _conflict_hunks import (  # pylint: disable=wrong-import-position
    has_markers,
)
from _git_io import (  # pylint: disable=wrong-import-position
    bind_repo,
)
from _merged_modes import (  # pylint: disable=wrong-import-position
    merged_mode_of,
)
from _taken_whole import (  # pylint: disable=wrong-import-position
    TakenWhole,
    dropped_change,
    single_merge_base,
    take_of,
    worktree_object,
)
from prompts import (  # pylint: disable=wrong-import-position
    SILENT_RETRY_NOTICE,
    VERDICT_RETRY_NOTICE,
    taken_whole_notice,
)


def _current_text(run, file_index: int, file: str) -> bytes:
    """FILE's content as fan-out RUN has it NOW: the spliced merge for a sidecar
    path that produced one, else the working file."""
    merged = Path(run.merged_path(file_index))
    if file in run.sidecar and merged.is_file() and merged.stat().st_size:
        return merged.read_bytes()
    return Path(file).read_bytes()


def residue_files(run, answered: set[str]) -> list[str]:
    """The files that still carry markers after RUN spliced in what resolved,
    minus ANSWERED — the set no retry can improve on.

    A file whose blocks all resolved is absent, so the retry never re-reads an
    answer the run already has. A file with an ERRORED shard is absent too — a
    dead credential, a 429, a crash — so the credential ladder reruns the whole
    fan-out on the next rung, and a retry here would buy the identical refusal.
    A file whose shard recorded a DECLINE is absent for the same reason: the
    model reached a judgement, so a retry buys the same judgement again.

    A file whose earlier shard was ALREADY whole-file is NOT absent, and that is
    what this pass exists for: a shard that ran, reported success and delivered
    nothing has answered nothing, so repeating the assignment is not repeating
    an answer — a large file whose shard spent its output budget looks exactly
    like this, and refusing it would discard every other file's resolution.

    A modify/delete path answers with a verdict, not a marker-free file, so it is
    residue exactly when ANSWERED lacks it: git leaves no marker to read there."""
    return [
        file
        for file_index, file in enumerate(run.files)
        if file not in answered
        and (
            file in run.modify_delete
            or has_markers(_current_text(run, file_index, file))
        )
    ]


def run_residue_pass(run, summaries: list[dict]) -> list[dict]:
    """Retry what is still conflicted, keeping every block already resolved, and
    return the retries' summaries. SUMMARIES is what the first pass produced,
    read for which files errored or declined.

    Without this, a $3 resolve that got 4 of 5 blocks right lands exactly what a
    resolve that got none did: bundle refuses the whole merge over the leftover
    markers. A retry that answers nothing too makes its file silent twice, which
    `summarize_second_runs` records."""
    residue = residue_files(
        run,
        {
            s["file"]
            for s in summaries
            if s["is_error"]
            or s["declined"]
            or (s["resolved"] and s["file"] in run.modify_delete)
        },
    )
    if not residue:
        return []
    if run.wait_available() <= 0:
        print(
            "::warning::the fan-out's budget is spent, so the still-conflicted "
            f"file(s) get no whole-file retry: {', '.join(residue)}",
            file=sys.stderr,
        )
        return []
    print(
        f"::notice::retrying {len(residue)} still-conflicted file(s) whole, "
        f"keeping every block already resolved: {', '.join(residue)}",
        file=sys.stderr,
    )
    retries = run.run_more(
        [
            run.second_run(
                f,
                VERDICT_RETRY_NOTICE if f in run.modify_delete else SILENT_RETRY_NOTICE,
            )
            for f in residue
        ]
    )
    return run.summarize_second_runs(retries)


def answered_takes(run) -> dict[str, tuple[Path, TakenWhole]]:
    """Every file of fan-out RUN whose marker-free answer is a one-sided take,
    with the path holding that answer: the spliced or delivered file, never a
    block shard's fragment. Empty when the tree is not mid-merge.

    A modify/delete path answers keep-or-delete, and a relocation's answer IS
    one side's stub, so neither is judged."""
    heads = run_git("rev-parse", "HEAD", "MERGE_HEAD")
    if heads.returncode != 0:
        return {}
    bind_repo(Path.cwd())
    ours, theirs = heads.stdout.split()
    base = single_merge_base(ours, theirs)
    if base is None:
        return {}
    takes: dict[str, tuple[Path, TakenWhole]] = {}
    for file_index, file in enumerate(run.files):
        if file in run.modify_delete or file in run.relocated:
            continue
        answer = run.resolution_of(file_index, file) if file in run.sidecar else file
        if answer is None:
            continue
        data = Path(answer).read_bytes()
        if not data or has_markers(data):
            continue
        # The mode bundle will stage, so a rewrite that lost the executable bit
        # still reads as the parent's bytes.
        at_merge = worktree_object(
            file, Path(answer), merged_mode_of(file, base, ours, theirs)
        )
        take = take_of(at_merge, (ours, theirs), base, file)
        if take is not None:
            takes[file] = (Path(answer), take)
    return takes


def reask_taken_whole(run) -> list[dict]:
    """Ask each file `answered_takes` names ONCE more, with the dropped side's
    diff, and return those second runs' summaries.

    A second run never costs the first answer: one that leaves its target empty
    or conflicted gets the first answer put back. A second one-sided answer
    stands, so the post-merge check still reports and repairs it."""
    takes = answered_takes(run)
    if not takes:
        return []
    if run.wait_available() <= 0:
        print(
            "::warning::the fan-out's budget is spent, so no one-sided answer is "
            f"asked again: {', '.join(takes)}",
            file=sys.stderr,
        )
        return []
    print(
        f"::notice::asking {len(takes)} file(s) again, each answered with one "
        f"parent's whole file while the other parent changed it: {', '.join(takes)}",
        file=sys.stderr,
    )
    first = len(run.work)
    works = [
        run.second_run(file, taken_whole_notice(take, dropped_change(take, file)))
        for file, (_, take) in takes.items()
    ]
    answers = [path.read_bytes() for path, _ in takes.values()]
    # Where each second run delivers. A sidecar path's run starts from the first
    # answer too, so "leave it as it is" means the same thing for both kinds.
    targets = [
        Path(run.resolved_path(first + offset) if run.delivers_out(work) else work.path)
        for offset, work in enumerate(works)
    ]
    for target, answer in zip(targets, answers):
        target.write_bytes(answer)
    retries = run.run_more(works)
    for (index, work), target, answer in zip(retries, targets, answers):
        if not run.delivered_resolution(index, work):
            target.write_bytes(answer)
    return run.summarize_second_runs(retries)
