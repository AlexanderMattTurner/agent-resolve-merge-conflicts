"""The pre-push self-review, run over the still-local merge commit.

Runs self_review.py the way the post-push watchdog reads the pushed delta,
interprets its exit codes, re-verifies whatever its fixer amended, and records
a completed read on the Bundle for reuse-bundle.py's marker. bundle.py owns
whether the review runs at all (the caller's opt-in, the credential ladder);
this module owns everything after that decision.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from _git_io import git, git_lines, git_status
from _post_merge_check import run as run_post_merge_check
from _refusal import fail, report_block

if TYPE_CHECKING:
    from bundle import Bundle

_SCRIPT_DIR = Path(__file__).resolve().parent

# The reviewer's CANNOT-VERIFY status, which is a different report from its
# flagged-the-resolution status. Exit 3 is a third: flagged, with NO fix round
# attempted, because the credential ladder spent the budget.
_SELF_REVIEW_CANNOT_VERIFY = 2
_SELF_REVIEW_FLAGGED_UNATTEMPTED = 3


def review_and_verify(
    step: "Bundle",
    *,
    tokens: list[str],
    verify_regenerated: str,
    pre_pass_verified: str,
    untrusted: bool,
) -> None:
    """Run the reviewer, act on its verdict, and re-verify any fixer amend."""
    before = git("rev-parse", "HEAD").strip()
    # Pinned here rather than left to self_review's own default, so the two
    # agree on where the findings land and this step can keep them.
    review_dir = Path(
        os.environ.get("SELF_REVIEW_DIR")
        or f"{os.environ.get('RUNNER_TEMP') or '/tmp'}/self-review"  # noqa: S108
    )
    done = subprocess.run(
        ["python3", str(_SCRIPT_DIR / "self_review.py")],
        env={
            **os.environ,
            "SELF_REVIEW_DIR": str(review_dir),
            "SELF_REVIEW_TOKEN_LADDER": "\n".join(tokens),
            "AUTO_RESOLVE_VERIFY_REGENERATED": verify_regenerated,
            "AUTO_RESOLVE_PRE_PASS_VERIFIED": pre_pass_verified,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    output = done.stdout + done.stderr
    if done.returncode != 0:
        print(output, end="" if output.endswith("\n") else "\n", file=sys.stderr)
        # Exit 2 (CANNOT-VERIFY) says nothing about the resolution, so it never
        # takes the exit-1 branch below, which judges it bad. Discarding here spends
        # the whole fan-out to punish a rate-limited credential ladder and leaves the
        # conflict for the next scan to buy again. It lands flagged instead, and
        # claude-review.yaml reads the same delta, so this pre-push read is never alone.
        if done.returncode == _SELF_REVIEW_CANNOT_VERIFY:
            step.unverified = True
            print(
                "::warning::the merge-delta reviewer produced no verdict, so "
                "this resolution lands UNVERIFIED: auto-merge is disabled and "
                "a human reads it before it merges."
            )
            return
        # Exit 3 is the same verdict with a different CAUSE: the reviewer
        # flagged the resolution and no fix round fit in the wall-clock budget,
        # so no correction ran. Saying one "could not satisfy the reviewer"
        # there describes a correction that never happened.
        findings = keep_the_findings(review_dir)
        if done.returncode == _SELF_REVIEW_FLAGGED_UNATTEMPTED:
            fail(
                "the resolved merge was flagged by the merge-delta reviewer, "
                "and no fix round fit in its wall-clock budget",
                "the resolution introduced content traceable to neither parent, "
                "and NO automatic correction was attempted: no fix round fit in "
                "this step's wall-clock budget.",
                report=findings,
            )
        fail(
            "the resolved merge was still flagged by the merge-delta "
            "reviewer after its fix rounds",
            "the resolution introduced content traceable to neither parent, "
            "and the automatic correction could not satisfy the reviewer.",
            report=findings,
        )
    print(output, end="" if output.endswith("\n") else "\n")
    if git("rev-parse", "HEAD").strip() != before:
        verify_the_fixers_output(step, before, untrusted=untrusted)
    step.reviewed = True


def keep_the_findings(review_dir: Path) -> str:
    """The reviewer's findings, copied into the uploaded bundle and rendered for
    the refusal comment.

    Both records this refusal leaves are erased: the run log ages out, and the
    sticky comment is one per pull request, so the next run overwrites it. The
    findings then survive nowhere, and the class the reviewer refused on cannot
    be acted on by anyone (glovebox #4426)."""
    review = review_dir / "merge-review.md"
    if not review.exists():
        return ""
    text = review.read_text(encoding="utf-8")
    kept = Path(os.environ["BUNDLE_DIR"]) / "merge-review.md"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text(text, encoding="utf-8")
    return report_block(text)


def verify_the_fixers_output(step: "Bundle", before: str, *, untrusted: bool) -> None:
    """Re-run verify_resolved_content over the resolved set widened by whatever
    the self-review fixer touched, so its bytes are not the one content path into
    the bundle that no lint judges."""
    touched = git_lines("diff", "--name-only", before, "HEAD")
    # Minus paths the fixer deleted: pre-commit dies on a filename it cannot open.
    step.staged = [
        name for name in sorted(set(step.staged) | set(touched)) if Path(name).exists()
    ]
    step.verify_resolved_content()
    # Both whole-tree post-conditions ran BEFORE the review, so a fixer amend
    # was the one content path into the bundle neither re-judged. self_review
    # restores a generated file the fixer rewrote; this is what makes that
    # restore checkable here rather than trusted.
    step.verify_generated_artifacts()
    # Re-derived, never carried forward: the ranges the first pass measured
    # index a tree the fixer has since rewritten, and a line the FIXER put
    # outside a span was never in that list at all. This is the only report
    # `land` cannot re-derive, so a stale one names lines nobody wrote.
    step.out_of_conflict_rewrites = []
    step.revert_out_of_conflict_rewrites()
    # Overwrites the earlier finding rather than adding to it: the fixer rewrote
    # the tree, so this run is the current answer about the bytes that ship.
    step.post_merge_finding = run_post_merge_check(
        untrusted_head=untrusted,
        repair=step.repair_post_merge_once,
        head_sha=step.checked_out_head,
        base_sha=step.merge_base_side,
        deadline=step.post_merge_deadline(),
    )
    if git_status("diff", "--cached", "--quiet") != 0:
        print(git("commit", "--amend", "--no-edit", "--no-verify"), end="")
