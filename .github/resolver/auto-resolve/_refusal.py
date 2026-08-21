"""How the auto-resolve BUNDLE step gives up and tells a human.

Every check in bundle.py ends here when it refuses: the head is marked handed off,
the tree goes back, the pull request gets a comment naming the cause, and the step
exits non-zero so nothing is bundled. The mark is what stops the resolver paying for
the identical wall on every later base push; a PERMANENT refusal also labels the
pull request, which drops it from every later scan whatever its head does.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    abort_merge_if_in_progress,
)
from _pr_sweep import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Gh,
    live_head_moved,
)


# The closing sentence a human handoff carries when the HARNESS fell short, here
# and in land.sh's `fail`. The reader's next act should be a bug report, not a
# quiet hand resolution that leaves the same wall standing for the next PR.
HANDOFF_IS_A_DEFECT = (
    "Leaving the conflict for a human to resolve. A handoff is a DEFECT in "
    "auto-resolve, never a normal outcome: the run that writes this sentence "
    "failed at the job it exists to do, so report it as a bug against the "
    "resolver rather than treating it as the expected fallback."
)

# The closing sentence a DECLINE carries instead. It still ASKS FOR THE EVIDENCE,
# which is the whole point of the sentence above: the run log ages out and the next
# run overwrites this comment, so a decline nobody records is a resolver weakness
# nobody can ever act on. What it drops is the false claim that the run failed at
# its job — the model judged these hunks, and this run's verdict stands either way.
DECLINE_IS_A_VERDICT = (
    "Leaving the conflict for a human to resolve. This is the resolver's VERDICT on "
    "these hunks, not a harness failure, so resolve them by hand — no resolver fix "
    "re-opens THIS run. Then record what a better resolver would have needed: the "
    "run log ages out and the next run overwrites this comment, so that evidence "
    "has no other home."
)


def _flush_inherited_stdio() -> None:
    """Flush Python's buffered stdout/stderr before a subprocess that inherits them.

    INVARIANT — this ordering is what a byte-level golden record depends on: the
    child writes straight to the shared fd while `print()` sits in Python's own
    buffer, so under a piped stdout (every non-tty caller, including this file's
    own test corpus) the child's line can land before an earlier `print()` that
    logically preceded it.
    """
    sys.stdout.flush()
    sys.stderr.flush()


def superseding_head() -> str:
    """The pull request's head on GitHub, when a push replaced the commit this run read.

    The commit this run read is HEAD_SHA — the SHA discover put in the matrix and
    the job checked out. Deriving it from the local HEAD instead cannot tell a
    merge THIS run made from a merge the pull request's own head already was, so
    a head that is itself a merge commit reads as "moved to" the very SHA the job
    was dispatched with, and the refusal comment is dropped for a push nobody
    made. `live_head_moved` is the one definition of the question and takes the
    SHA as an argument for exactly that reason.
    """
    dispatched = os.environ.get("HEAD_SHA", "")
    if not dispatched:
        # Every other doubt here answers "" and lets the comment through, and so
        # does this one: suppressing a refusal needs evidence of a push, and an
        # unset variable is evidence of nothing. Loud, because the resolve job
        # sets it on every step and a run without it is misconfigured.
        print("::warning::HEAD_SHA is unset, so no push can be ruled in or out.")
        return ""
    return live_head_moved(
        Gh(repo=os.environ["GH_REPO"], tool="auto-resolve"),
        os.environ["PR"],
        dispatched,
    )


def escalation_block(paths: list[str], said: str) -> str:
    """The copy-pasteable prompt a JUDGEMENT handoff hands the reader.

    A refusal that names a decision nobody made leaves the reader to rebuild the
    context this run already holds: which branches, which paths, and what the
    resolver would not decide. This block carries all three into whichever model
    they ask, so the question they answer is the question the resolver asked.

    The block is a HANDOVER, not a conversation: the reader pastes it into a
    fresh session and leaves. So it tells that session to decide and to record
    the decision, never to ask the intent behind a side.

    Only a refusal that hands over a DECISION gets one. A plumbing fault, a
    denied grant or a spent wall clock has a remedy, not a judgement call."""
    repo = os.environ.get("GH_REPO", "")
    pr = os.environ.get("PR", "")
    head = os.environ.get("HEAD_REF", "the pull request branch")
    base = os.environ.get("BASE_REF", "the base branch")
    named = ", ".join(paths)
    return (
        "**This needs a higher-level decision**, and an automated merge cannot "
        "make it: both sides are defensible, so the answer depends on what the "
        "change is FOR. Paste this into your AI, with the two versions of the "
        "file(s) in front of it:\n\n"
        "```\n"
        f"I am merging branch {head} into {base} in {repo} (PR #{pr}). "
        f"A merge conflict in {named} is unresolved.\n\n"
        f"What the automated resolver would not decide: {said}\n\n"
        "Read both sides of the conflict, then resolve it yourself. This prompt "
        "is the whole handover: whoever pasted it is gone and answers nothing. "
        "Do not ask about the intent behind either side, and do not wait for a "
        "reply. Where the two sides disagree about behaviour, name the decision, "
        "then combine both sides so each change still does its job. Take one "
        "side alone only when the two cannot both hold. State the choice and its "
        "alternative in your answer. If you have the repository, also record "
        "them on the pull request, and run the tests that cover the conflicted "
        "code, naming them.\n"
        "```"
    )


def fail(
    error: str,
    comment: str,
    *,
    resolver_fault: bool = False,
    declined: bool = False,
    escalate: str = "",
) -> NoReturn:
    """Publish this run's refusal and stop.

    Marks the head first, because the DEFAULT refusal is a verdict about the merge
    that a re-run reproduces: the resolve steps have already run by the time
    bundle.py calls this, so the money is spent and the next scan would hand the
    model the same tree. The mark is void when the run bought nothing — the release
    the ladder writes on an all-rungs-dead run cancels it, and `hold_on`
    reads the two together.

    ``resolver_fault`` is the opposite case and takes no mark: the refusal blames
    the resolver's OWN grants, tooling or workflow plumbing, so the fix lands
    outside this pull request and a re-run against this same head then answers
    differently. Marking one of those would strand the head until a human pushed
    to it, which is the failure the attempt mark's TTL exists to avoid.

    ``declined`` says the mark records the MODEL's verdict on these hunks rather
    than the harness falling short, so discover holds it through a resolver change
    instead of retiring it — see mark-handoff.sh. ``escalate`` carries the
    copy-pasteable prompt from :func:`escalation_block`, for the refusals that
    hand over a decision rather than a remedy."""
    print(f"::error::{error}")
    # mark_handed_off's child process writes straight to this fd; stdout to a
    # pipe is block-buffered, so without this flush its write can land before
    # the line above's, printing the mark ahead of the error it explains.
    sys.stdout.flush()
    if not resolver_fault:
        mark_handed_off(declined=declined)
    abort_merge_if_in_progress()
    # A push landed while this run was resolving, so the diagnosis above is about a
    # commit that is no longer the pull request's head, and `land` could not have put
    # this resolution on top of the new one either. The commonest cause is a human who
    # resolved the conflict by hand, and telling that human a conflict is waiting is the
    # one thing this path must not do. The job stays red, so the diagnosis is on the run.
    if superseded := superseding_head():
        print(
            f"::warning::{os.environ.get('HEAD_REF', 'the PR branch')} moved to "
            f"{superseded} while this run was resolving, so no comment is posted: "
            "this failure is about a commit that is no longer the head."
        )
        raise SystemExit(1)
    # Published as THIS run's verdict, replacing the "working on it" comment the run
    # posted before it spent anything. Through the sibling shell entry point rather
    # than a second `gh pr comment` here: one definition of the sticky comment, so the
    # PR carries one auto-resolve comment however the run ends.
    _flush_inherited_stdio()
    subprocess.run(
        ["bash", str(Path(__file__).resolve().parent / "status-comment.sh")],
        env={
            **os.environ,
            "STATE": "verdict",
            "BODY": f"⚠️ **Auto-resolve could not finish** — {comment} "
            f"{DECLINE_IS_A_VERDICT if declined else HANDOFF_IS_A_DEFECT}"
            + (f"\n\n{escalate}" if escalate else ""),
        },
        check=False,
    )
    raise SystemExit(1)


def mark_handed_off(*, declined: bool = False) -> None:
    """Mark this head as handed to a human, so no later scan re-buys the verdict.

    PROBLEM CLASS — paying an LLM again for an answer whose inputs did not change.

    This mark is what bounds the spend on a conflict a paid run already gave up on:
    the attempt mark discover reads expires, and a push to the base re-enables the PR
    one floor-hour later, so a repository merging dozens of times a day pays for the
    same wall every hour until the head moves. Only a push to the head clears it.
    Best-effort by design, and through the shell entry point so the mark has ONE
    writer: failing to mark must not swallow the diagnosis the caller is publishing.
    """
    _flush_inherited_stdio()
    if subprocess.run(
        ["bash", str(Path(__file__).resolve().parent / "mark-handoff.sh")],
        env={
            **os.environ,
            "REPO": os.environ.get("GH_REPO", ""),
            "AUTO_RESOLVE_DECLINE": "true" if declined else "",
        },
        check=False,
    ).returncode:
        # Silence here reads exactly like the pre-mark world — the same verdict
        # re-bought every floor-hour — with nothing saying the bound was lost.
        print(
            "::warning::could not mark this head handed off, so a later scan may "
            "re-buy this verdict."
        )


def apply_blocked_label(pr_number: str, label: str, tool: str) -> None:
    """Label the PR so the tool's own later scans skip it.

    This refusal is what bounds the spend on a permanent failure: without it,
    every push to the base branch re-flips the PR to CONFLICTING and re-runs a
    paid model resolve into the identical wall, forever. Best-effort by design:
    failing to label must not mask the underlying error the caller is already
    reporting.
    """
    _flush_inherited_stdio()
    subprocess.run(
        [
            "gh",
            "label",
            "create",
            label,
            "--color",
            "e4e669",
            "--force",
            "--description",
            f"{tool} cannot resolve this PR; remove the label to let it retry",
        ],
        check=False,
    )
    subprocess.run(["gh", "pr", "edit", pr_number, "--add-label", label], check=False)


if __name__ == "__main__" and sys.argv[1:] == ["--handoff-sentence"]:
    # land.sh's `fail` reads the sentence from here, so the two entry points
    # cannot drift apart in wording.
    print(HANDOFF_IS_A_DEFECT)
    raise SystemExit(0)
