"""How the auto-resolve BUNDLE step gives up and tells a human.

Every check in bundle.py ends here when it refuses: the head is marked handed off,
the tree goes back, the pull request gets a comment naming the cause, and the step
exits non-zero so nothing is bundled. The mark is what stops the resolver paying for
the identical wall on every later base push; a PERMANENT refusal also labels the
pull request, which drops it from every later scan whatever its head does.
"""

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    abort_merge_if_in_progress,
)
from _handoff_cause import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    mark_should_decline,
    suffix as cause_suffix,
)
from _pr_sweep import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Gh,
    live_head_moved,
)
from prompts import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    keep_both_ends,
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


# What a refusal quotes UNFOLDED. A tail, because a report's head is usually its banner
# — but only as the preview: the whole report goes under the fold below it.
REPORT_TAIL_LINES = 20
REPORT_TAIL_CHARS = 2000
# The cap on each RENDERED block, fences included. A pull request comment holds 65536
# characters, GitHub rejects the whole comment past it, and the sentences around the
# two blocks need the rest. The preview's own cap is the tail plus room for its
# dropped-output line and its fences.
REPORT_FULL_CHARS = 30000
_PREVIEW_BLOCK_CHARS = 2 * REPORT_TAIL_CHARS


# What a quoted report says in place of a credential, and which environment names hold
# one. A name test rather than a value test: a rung added to the credential ladder is
# covered without an edit here, and no heuristic decides what a secret looks like.
REDACTED = "[redacted]"
_CREDENTIAL_NAME = re.compile("KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE|CREDENTIAL")
_SHORTEST_CREDENTIAL = 8
# The two control characters a report legitimately holds. Every other one is dropped.
_KEEP = ("\n", "\t")


def _publishable(text: str) -> str:
    """The bytes of a command's output that may go on a public pull request.

    INVARIANT — this is what keeps a model credential off the pull request. The check
    and the hooks a caller names are defined by the pull request's own head, and the
    resolve job runs them with every credential in the environment. The job log masks a
    registered secret; a comment masks nothing, so the value is replaced here.

    A control character goes for a second reason: this text crosses into a child
    process's ENVIRONMENT as `BODY`, and a NUL byte there raises `ValueError`, which
    would lose the whole refusal rather than one line of its report.
    """
    for name, value in os.environ.items():
        if len(value) >= _SHORTEST_CREDENTIAL and _CREDENTIAL_NAME.search(name):
            text = text.replace(value, REDACTED)
    return "".join(char for char in text if char in _KEEP or char.isprintable())


def report_block(text: str) -> str:
    """A failing command's own output, fenced for the handoff comment.

    A comment that names a check and quotes nothing sends every reader to the job log,
    and the resolver's runs are not distinguishable on the Actions list — so that log
    costs a search rather than a click. Empty for a command that printed nothing, where
    a fence around no bytes says less than the sentence above it already does.

    The tail is the PREVIEW only, and the whole report goes under a fold beneath it.
    A tail alone quotes whichever tool printed last, which is rarely the tool that
    failed — `keep_both_ends` names that class. So the reader who needs an earlier
    line clicks the fold instead of hunting the run log, and only a report past
    `REPORT_FULL_CHARS` still loses anything.

    A quote that dropped anything says so, because an unmarked cut reads as the whole
    report.
    """
    report = _publishable(text).strip()
    if not report:
        return ""
    lines = report.splitlines()
    tail = "\n".join(lines[-REPORT_TAIL_LINES:])[-REPORT_TAIL_CHARS:]
    if tail == report:
        return f"What it reported:\n\n{_fenced(report, _PREVIEW_BLOCK_CHARS)}"
    preview = f"[…earlier output dropped; the whole report is under the fold]\n{tail}"
    return (
        f"What it reported, last:\n\n{_fenced(preview, _PREVIEW_BLOCK_CHARS)}\n\n"
        f"<details><summary>The whole report ({len(lines)} lines)</summary>\n\n"
        f"{_fenced(report, REPORT_FULL_CHARS)}\n\n</details>"
    )


def _fenced(text: str, cap: int) -> str:
    """The text in a code fence, with the WHOLE block inside `cap` characters.

    The fence is one backtick longer than the longest run the text holds, so a report
    that quotes a fenced block of its own cannot end this one early and spill the rest
    as prose. That makes the DELIMITERS as long as the text can make them, so the cap
    is spent on the pair FIRST and the text takes what is left: a comment past
    GitHub's own limit is rejected whole, and publishes nothing at all.

    Cutting the text can only shorten the fence it needs, never lengthen it, so the
    loop settles — and it stops early where a shorter cut buys no shorter fence."""
    body = text
    fence = "`" * max(3, _longest_backtick_run(body) + 1)
    while len(body) + 2 * len(fence) + 2 > cap:
        body = keep_both_ends(body, max(cap - 2 * len(fence) - 2, 0))
        shorter = "`" * max(3, _longest_backtick_run(body) + 1)
        if len(shorter) == len(fence):
            break
        fence = shorter
    return f"{fence}\n{body}\n{fence}"


def _longest_backtick_run(text: str) -> int:
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    return longest


def fail(
    error: str,
    comment: str,
    *,
    resolver_fault: bool = False,
    declined: bool = False,
    escalate: str = "",
    report: str = "",
    closing: str = "",
    cause: str = "",
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
    hand over a decision rather than a remedy. ``report`` carries the failing
    command's own output from :func:`report_block`, and ``closing`` replaces the
    closing sentence when neither standing one fits. ``cause`` names what this run
    ran out of, so the NEXT run on this head can tell a repeat from a first one."""
    print(f"::error::{error}")
    # mark_handed_off's child process writes straight to this fd; stdout to a
    # pipe is block-buffered, so without this flush its write can land before
    # the line above's, printing the mark ahead of the error it explains.
    sys.stdout.flush()
    # ASKED BEFORE THE MARK. A push landed mid-run, so the diagnosis above is about a
    # commit that is no longer the head. Telling a human who just resolved it by hand
    # that a conflict is waiting is the one thing this path must not do, and a mark
    # here spends the head's one retry on a verdict about a tree nobody has.
    if superseded := superseding_head():
        print(
            f"::warning::{os.environ.get('HEAD_REF', 'the PR branch')} moved to "
            f"{superseded} while this run was resolving, so no comment is posted and "
            "no mark is written: this failure is about a commit that is no longer the "
            "head, and the next scan resolves the new one."
        )
        abort_merge_if_in_progress()
        raise SystemExit(1)
    if not resolver_fault:
        mark_handed_off(declined=declined, cause=cause)
    abort_merge_if_in_progress()
    # Published as THIS run's verdict, replacing the "working on it" comment the run
    # posted before it spent anything. Through the sibling shell entry point rather
    # than a second `gh pr comment` here: one definition of the sticky comment, so the
    # PR carries one auto-resolve comment however the run ends.
    _flush_inherited_stdio()
    body = (
        f"⚠️ **Auto-resolve could not finish** — {comment} "
        f"{closing or (DECLINE_IS_A_VERDICT if declined else HANDOFF_IS_A_DEFECT)}"
        + (f"\n\n{report}" if report else "")
        + (f"\n\n{escalate}" if escalate else "")
    )
    subprocess.run(
        ["bash", str(Path(__file__).resolve().parent / "status-comment.sh")],
        env={**os.environ, "STATE": "verdict", "BODY": body},
        check=False,
    )
    if resolver_fault:
        _tell_whoever_owns_the_plumbing(error, body)
    raise SystemExit(1)


def _run_url() -> str:
    """This run's page, from `lib/run-url.bash`'s one definition of that link.

    Sourced rather than re-derived: a second spelling of the URL drifts from the
    one every commit-status mark already carries.
    """
    done = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; auto_resolve_run_url',
            "_",
            str(Path(__file__).resolve().parents[1] / "lib" / "run-url.bash"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip()


def _tell_whoever_owns_the_plumbing(error: str, body: str) -> None:
    """Repeat a PLUMBING refusal on the issue the caller named for them.

    INVARIANT — this is what gives a `resolver_fault` refusal a reader who can
    act on it. Its only other surface is the pull request's sticky comment,
    which the next run on that PR overwrites, and which is read by whoever owns
    the BRANCH. A broken pin, grant or tool is not theirs to fix, so that
    comment reaches nobody who can, and the evidence is gone before anyone
    looks.

    One comment per refusal, deliberately: the plumbing is broken for every
    conflicted pull request at once, so the volume tracks the damage. A caller
    that names no issue keeps the sticky comment alone.

    `check=False` for the same reason `status-comment.sh` is: a refusal must
    publish its diagnosis even when this extra notice cannot be written.
    """
    issue = os.environ.get("AUTO_RESOLVE_PLUMBING_ISSUE", "").strip()
    if not issue:
        return
    pr = os.environ.get("PR", "?")
    notice = (
        f"<!-- auto-resolve-plumbing -->\n"
        f"**Auto-resolve stopped on its own plumbing** — {error}\n\n"
        f"This is not the pull request's defect, so PR #{pr} carries a comment "
        "nobody who can fix it reads. The fix lands in the workflow, its pins or "
        f"its grants.\n\nThe run: {_run_url()}\n\n{body}"
    )
    subprocess.run(
        ["gh", "issue", "comment", issue, "--body", notice],
        env={**os.environ},
        check=False,
        capture_output=True,
    )


# How long the drain below may wait for the killed group's pipes to close. A
# member that made ITSELF a new session escapes the signal and holds the write end
# open, and an unbounded read there spends the wall clock the kill just saved.
_DRAIN_SECONDS = 10.0

# How long a straggler gets to leave on SIGTERM before SIGKILL. A git call takes
# milliseconds, so this is the pre-pass's own child processes, not the index.
_REAP_SECONDS = 5.0


def _group_alive(pgid: int) -> bool:
    """Whether any process is still running in the group PGID leads."""
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def reap_group(pgid: int) -> None:
    """End anything a finished command left running in its own process group.

    PROBLEM CLASS — a command that exits does not take its children with it. A
    derived-file pre-pass that fans generators out and returns on the first
    failure leaves the rest running, and a generator's last act is `git add`: the
    straggler takes `.git/index.lock` seconds later, and the next git call in the
    same step dies with "Another git process seems to be running in this
    repository". Call this only once the direct child is REAPED, or its own zombie
    reads as a live group member and every call waits out the grace below.
    """
    if not _group_alive(pgid):
        return
    print(
        f"::warning::the command left processes running after it exited; ending "
        f"process group {pgid}, so no straggler of it can write this checkout's index."
    )
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + _REAP_SECONDS
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def run_bounded(
    argv: list[str], timeout: float | None, *, cwd: str | None = None
) -> subprocess.CompletedProcess:
    """`subprocess.run`, plus the process-GROUP kill a timeout owes the caller.

    On a timeout `subprocess.run` kills the direct child and waits for that child
    alone, so every process the command started outlives the bound. An orphan can
    still WRITE, and the post-merge check compares the tree against the state it
    recorded before the run — a straggler that lands a file after the bound turns a
    clean merge into a refusal. A new session makes the command a group leader, so
    one signal reaches everything it started."""
    with subprocess.Popen(  # noqa: S603
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as overran:
            # `start_new_session` makes the child its own group LEADER, so the
            # group id IS its pid. Reading it back with `os.getpgid` instead adds a
            # way to fail: the leader can exit while a child it started still holds
            # the pipes, and the lookup then raises with the group never signalled.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            # The group signal is SUPPRESSED on two errors, and neither says the
            # child died. `Popen.__exit__` then waits on it with no bound at all,
            # which spends the wall clock this whole function exists to save.
            proc.kill()
            # Onto the exception, because a killed check's partial output is what
            # says WHERE it hung, and its caller has nothing else to quote.
            with contextlib.suppress(subprocess.TimeoutExpired):
                overran.stdout, overran.stderr = proc.communicate(
                    timeout=_DRAIN_SECONDS
                )
            raise
    # OUTSIDE the `with`, so the direct child is already reaped: its own zombie
    # would otherwise read as a live group member for the whole grace period.
    reap_group(proc.pid)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def run_or_refuse(
    argv: list[str],
    *,
    label: str,
    input_name: str,
    lost: str,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """The CALLER's command, output captured — or a plumbing refusal when the
    runner cannot run it at all. `lost` names what the refusal costs this run.

    `check=False` catches a non-zero EXIT and nothing else, and no shell stands
    between this and the command: a binary the runner cannot execute raises here
    rather than reporting 126 or 127. An uncaught raise loses a resolution the
    model has already been billed for, and reads exactly like a merge the
    resolver could not do.

    `timeout` bounds the caller's command in wall clock, and `TimeoutExpired`
    RAISES past this: only the caller knows whether an overrun costs the run or
    is one term of a budget it holds. A command with no bound can spend the whole
    job.

    `resolver_fault=True` leaves the head UNMARKED, so a re-run after the caller
    fixes its own wiring reaches this same head instead of waiting out the
    attempt mark's TTL.
    """
    try:
        return run_bounded(argv, timeout)
    except OSError as exc:
        fail(
            f"the {label} '{argv[0]}' will not run on this runner",
            f"this run resolved the conflict and then could not {lost}: "
            f"`{input_name}` starts with `{argv[0]}`, which this runner cannot "
            f"execute ({exc}) — this job installs no such binary, or the file it "
            "names is not executable. Nothing was landed, and the resolution is "
            "not lost — fix that in the calling workflow and re-run, and this "
            "same head resolves.",
            resolver_fault=True,
        )


def mark_handed_off(*, declined: bool = False, cause: str = "") -> None:
    """Mark this head as handed to a human, so no later scan re-buys the verdict.

    PROBLEM CLASS — paying an LLM again for an answer whose inputs did not change.

    This mark is what bounds the spend on a conflict a paid run already gave up on:
    the attempt mark discover reads expires, and a push to the base re-enables the PR
    one floor later, so a repository merging dozens of times a day pays for the
    same wall once per floor until the head moves. Only a push to the head clears it.
    Best-effort by design, and through the shell entry point so the mark has ONE
    writer: failing to mark must not swallow the diagnosis the caller is publishing.

    A handoff for a cause this head ALREADY handed off on is written as a DECLINE
    instead. Nothing the resolver reads changed between the two, so the second run
    stopped where the first one stopped — and discover holds a decline through the
    resolver change that retires a handoff. This is the only place the two marks
    are chosen between on anything but the caller's own verdict.
    """
    if cause and not declined and mark_should_decline(cause):
        print(
            f"::notice::this head already handed off for '{cause}', under the same "
            "resolver and the same tree, so this refusal is recorded as a DECLINE. "
            "A third run would buy the answer these two already gave."
        )
        declined = True
    _flush_inherited_stdio()
    if subprocess.run(
        ["bash", str(Path(__file__).resolve().parent / "mark-handoff.sh")],
        env={
            **os.environ,
            "REPO": os.environ.get("GH_REPO", ""),
            "AUTO_RESOLVE_DECLINE": "true" if declined else "",
            "AUTO_RESOLVE_HANDOFF_CAUSE_SUFFIX": cause_suffix(cause),
        },
        check=False,
    ).returncode:
        # Silence here reads exactly like the pre-mark world — the same verdict
        # re-bought once per floor — with nothing saying the bound was lost.
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
