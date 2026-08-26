#!/usr/bin/env python3
"""Render the calling job's `permissions:` ceiling from the called workflow's jobs.

PROBLEM CLASS — a caller restates a permission set the callee already declares,
and the run dies before any job when the two drift.

GitHub lets a called workflow's jobs request only what the CALLING job already
holds. A caller that grants less does not go red job-by-job: the whole run ends
in `startup_failure`, so no check ever reports and every gate waits forever.
The ceiling is therefore a DERIVED value — the per-scope union of every job
`permissions:` block in the called workflow, where `write` beats `read` beats
absent — and this script owns the marked region that carries it. The
`gen-caller-permissions` pre-commit hook keeps it current; `--check` reports
drift and writes nothing, which is how CI asserts the committed region
round-trips.

Run with no argument to write, or `--check` to report drift and write nothing.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resolver"))
# pylint: disable=wrong-import-position  # must follow the sys.path insert above
from repolint._root import repo_root  # noqa: E402  (path inserted just above)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from lib_marked_region import (  # noqa: E402  (path inserted just above)
    region_begin,
    region_end,
    splice,
)

REPO_ROOT = repo_root(Path(__file__))
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# `none` is spelled out by a job that wants a scope explicitly withheld, and it
# raises the ceiling by nothing — the same as never naming the scope.
LEVELS = {"none": 0, "read": 1, "write": 2}


@dataclass(frozen=True)
class CallerCeiling:
    """One caller job that must hold the ceiling of the workflow it calls."""

    caller: Path  # the workflow holding the marked region
    callee: Path  # the reusable workflow whose jobs set the ceiling
    where: str  # the region label, repeated by both marker lines


CALLS = (
    CallerCeiling(
        caller=WORKFLOWS / "auto-resolve-conflicts.yaml",
        callee=WORKFLOWS / "auto-resolve.yaml",
        where="caller permissions ceiling for auto-resolve.yaml",
    ),
)


def _triggers_key(doc: dict) -> object:
    """PyYAML reads the bareword key `on:` as the boolean True (YAML 1.1)."""
    return doc.get("on", doc.get(True))


def job_permissions(doc: dict, path: Path) -> dict[str, dict[str, str]]:
    """{job id: its `permissions:` mapping} for every job that declares one.

    A job with no block inherits the workflow default, which cannot exceed what
    the caller already grants, so it raises the ceiling by nothing. A block
    spelled as a STRING (`read-all` / `write-all`) is refused rather than
    guessed at: it names every scope GitHub knows, including ones this repo has
    never granted, so expanding it here would silently widen the ceiling.
    """
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        raise ValueError(f"{path}: has no `jobs:` mapping to derive a ceiling from")
    found = {}
    for job_id, job in jobs.items():
        if not isinstance(job, dict) or "permissions" not in job:
            continue
        block = job["permissions"]
        if not isinstance(block, dict):
            raise ValueError(
                f"{path}: job `{job_id}` declares `permissions: {block!r}`; only a "
                "scope-by-scope mapping can be unioned into a caller's ceiling"
            )
        found[job_id] = block
    if not found:
        raise ValueError(f"{path}: no job declares `permissions:`")
    return found


def union(
    per_job: dict[str, dict[str, str]], path: Path
) -> dict[str, tuple[str, list[str]]]:
    """{scope: (winning level, the jobs that ask for it)} for every scope above `none`.

    Scopes come out sorted, so the rendered block does not depend on the order
    the callee happens to declare its jobs in.
    """
    best: dict[str, str] = {}
    for job_id, block in per_job.items():
        for scope, level in block.items():
            if level not in LEVELS:
                raise ValueError(
                    f"{path}: job `{job_id}` grants `{scope}: {level}`, which is "
                    f"none of {sorted(LEVELS)}"
                )
            if LEVELS[level] > LEVELS.get(best.get(scope, "none"), 0):
                best[scope] = level
    return {
        scope: (level, [j for j, block in per_job.items() if block.get(scope) == level])
        for scope, level in sorted(best.items())
        if LEVELS[level] > 0
    }


def render(per_job: dict[str, dict[str, str]], callee: Path, indent: str) -> str:
    """The ceiling block: one `scope: level` line per scope, plus who needs it.

    The trailing comment names the callee jobs that ask for that level, so a
    reviewer can check the grant against the file it is derived from instead of
    against prose. A ceiling with no scope at all would leave `permissions:` with
    an empty mapping, which YAML reads as null and GitHub then rejects — so an
    empty union is a refusal, not an empty region.
    """
    scopes = union(per_job, callee)
    if not scopes:
        raise ValueError(
            f"{callee}: every job grants `none`, so the rendered ceiling would be "
            "an empty mapping"
        )
    return "\n".join(
        f"{indent}{scope}: {level} # {callee.name}: {', '.join(holders)}"
        for scope, (level, holders) in scopes.items()
    )


def markers(call: CallerCeiling) -> tuple[str, str]:
    """The region's two marker lines, without their indentation."""
    return (
        region_begin(
            call.where,
            ".github/scripts/gen_caller_permissions.py",
            note="do not edit by hand",
        ),
        region_end(call.where),
    )


def region_indent(doc: str, begin: str, label: str) -> str:
    """The whitespace the region's begin marker sits at."""
    start = doc.find(begin)
    if start == -1:
        raise ValueError(f"{label}: begin marker not found: {begin}")
    return doc[doc.rfind("\n", 0, start) + 1 : start]


def render_caller(call: CallerCeiling, caller_text: str, callee_text: str) -> str:
    """CALLER_TEXT with the ceiling region re-rendered from CALLEE_TEXT.

    Pure: reads no file and writes none.
    """
    callee_doc = yaml.safe_load(callee_text)
    triggers = _triggers_key(callee_doc) if isinstance(callee_doc, dict) else None
    if not isinstance(triggers, dict) or "workflow_call" not in triggers:
        raise ValueError(
            f"{call.callee}: has no `workflow_call:` trigger, so no caller job "
            "holds a ceiling for it"
        )
    begin, end = markers(call)
    label = f"{call.caller}: {call.where}"
    per_job = job_permissions(callee_doc, call.callee)
    return splice(
        caller_text,
        begin=begin,
        end=end,
        block=render(per_job, call.callee, region_indent(caller_text, begin, label)),
        label=label,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift on stderr and write nothing; exit non-zero when a region is stale",
    )
    args = parser.parse_args()

    stale = []
    for call in CALLS:
        original = call.caller.read_text(encoding="utf-8")
        text = render_caller(call, original, call.callee.read_text(encoding="utf-8"))
        if text == original:
            continue
        stale.append(call.caller)
        if not args.check:
            call.caller.write_text(text, encoding="utf-8")
    if args.check and stale:
        names = "\n  ".join(str(p.relative_to(REPO_ROOT)) for p in stale)
        raise SystemExit(
            "the caller permission ceilings are stale in:\n  "
            f"{names}\nRun: uv run python .github/scripts/gen_caller_permissions.py"
        )


if __name__ == "__main__":
    main()
