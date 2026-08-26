"""The `review` and `note-skipped-review` jobs in claude-review.yaml decide, from
the event payload alone, whether a pull request gets a real automated review or
only the stand-in note that clears the review-findings gate's first leg. A PR
TITLE is written by the PR author, so a title-only skip lets an outside
contributor choose which one their PR gets.

These tests EVALUATE the two jobs' real `if:` expressions against synthetic
payloads rather than grepping them for a guard string, so a guard that is present
but wired into the wrong arm still reds. The evaluator below covers exactly the
expression subset those two conditions use; anything outside it raises rather
than guessing, so an `if:` that grows a new operator fails loudly here instead of
being silently mis-evaluated.
"""

import json
import os
import re
import subprocess
from functools import lru_cache

import pytest
import yaml

from tests._helpers import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-review.yaml"

SKIP_TITLES = [
    "chore: bump deps",
    "chore(deps): bump",
    "style: reformat",
    "style(css): reformat",
    "release: v1.2.3",
    "release(npm): v1.2.3",
]
REVIEWED_TITLES = ["feat: add a thing", "fix: a bug", "docs: rewrite the threat model"]
TRUSTED = ["OWNER", "MEMBER", "COLLABORATOR"]
UNTRUSTED = [
    "CONTRIBUTOR",
    "FIRST_TIME_CONTRIBUTOR",
    "FIRST_TIMER",
    "NONE",
    "MANNEQUIN",
]

REPO = "owner/repo"

# The conditions as they stood before the fork guard, kept verbatim so the
# invariants below can prove they REJECT the old wiring. Without this, a test
# asserting "an untrusted chore: PR is reviewed" would also pass against a
# workflow that reviewed every PR for some unrelated reason.
PRE_FIX_DECIDE = """
(
  github.event.pull_request.draft == false &&
  github.event.pull_request.user.type != 'Bot' &&
  !startsWith(github.event.pull_request.title, 'chore:') &&
  !startsWith(github.event.pull_request.title, 'chore(') &&
  !startsWith(github.event.pull_request.title, 'style:') &&
  !startsWith(github.event.pull_request.title, 'style(') &&
  !startsWith(github.event.pull_request.title, 'release:') &&
  !startsWith(github.event.pull_request.title, 'release(')
)
"""
PRE_FIX_NOTE = """
(github.event.action == 'opened' || github.event.action == 'ready_for_review') &&
github.event.pull_request.draft == false &&
(
  github.event.pull_request.user.type == 'Bot' ||
  startsWith(github.event.pull_request.title, 'chore:') ||
  startsWith(github.event.pull_request.title, 'chore(') ||
  startsWith(github.event.pull_request.title, 'style:') ||
  startsWith(github.event.pull_request.title, 'style(') ||
  startsWith(github.event.pull_request.title, 'release:') ||
  startsWith(github.event.pull_request.title, 'release(')
)
"""


# ── A small evaluator for the GitHub Actions expression subset in use ────────

_CONTEXT_PATH = re.compile(r"\bgithub(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def _lookup(context: dict, path: str):
    node = context
    for part in path.split("."):
        assert isinstance(node, dict) and part in node, (
            f"the workflow reads {path}, which the test payload does not model"
        )
        node = node[part]
    return node


def evaluate(expression: str, context: dict) -> bool:
    """Evaluate a GitHub `if:` expression against a payload.

    Supported: && || ! == != ( ), string literals, startsWith, contains,
    fromJSON, and `github.*` context paths. Anything else raises.
    """
    # YAML's `>-` already folds the real conditions onto one line; fold the
    # literals in this file the same way so both go through one code path.
    src = " ".join(expression.split())
    # `!=` must survive the `!` -> `not` rewrite, so park it first.
    src = src.replace("!=", "\x00")
    src = src.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    src = src.replace("\x00", "!=")
    src = _CONTEXT_PATH.sub(lambda m: f"_ctx({json.dumps(m.group(0))})", src)
    src = src.replace("startsWith(", "_starts_with(").replace("contains(", "_contains(")
    src = src.replace("fromJSON(", "_from_json(")
    # Everything the workflow may name is now a call or a literal; a bare
    # identifier means the expression used something this evaluator does not
    # model, and guessing at it would be worse than failing.
    outside_strings = re.sub(r"'[^']*'|\"[^\"]*\"", " ", src)
    leftover = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", outside_strings)) - {
        "_ctx",
        "_starts_with",
        "_contains",
        "_from_json",
        "and",
        "or",
        "not",
        "true",
        "false",
    }
    assert not leftover, f"unsupported tokens in the expression: {sorted(leftover)}"

    env = {
        "_ctx": lambda path: _lookup(context, path),
        "_starts_with": lambda text, prefix: str(text).startswith(prefix),
        "_contains": lambda haystack, needle: needle in haystack,
        "_from_json": json.loads,
        "true": True,
        "false": False,
    }
    return bool(eval(src, {"__builtins__": {}}, env))  # noqa: S307 - fixed inputs


def payload(
    *,
    action: str = "opened",
    title: str = "feat: a thing",
    association: str = "NONE",
    same_repo: bool = False,
    draft: bool = False,
    bot: bool = False,
    label: str = "",
) -> dict:
    return {
        "github": {
            "repository": REPO,
            "event": {
                "action": action,
                "label": {"name": label},
                "pull_request": {
                    "title": title,
                    "draft": draft,
                    "author_association": association,
                    "user": {"type": "Bot" if bot else "User"},
                    "head": {"repo": {"full_name": REPO if same_repo else "fork/repo"}},
                },
            },
        }
    }


@lru_cache(maxsize=None)
def _job_condition(job: str) -> str:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    condition = jobs[job].get("if")
    assert condition, f"job {job} lost its `if:` guard"
    return condition


def reviews(pl: dict) -> bool:
    return evaluate(_job_condition("review"), pl)


def notes(pl: dict) -> bool:
    return evaluate(_job_condition("note-skipped-review"), pl)


# ── The invariants ───────────────────────────────────────────────────────────


def test_the_opt_in_label_reaches_the_reviewer():
    """The skip notice tells a chore/style/release author to add
    `needs-auto-review`. The caller must admit that event, or the label starts
    no run and the notice promises a review nobody can request. Driven on the
    one PR shape the skip set really excludes: a TRUSTED author's chore title."""
    pl = payload(
        action="labeled",
        title="chore: bump a pin",
        association="OWNER",
        same_repo=True,
        label="needs-auto-review",
    )
    assert reviews(pl)


@pytest.mark.parametrize("label", ["documentation", "approved", ""])
def test_any_other_label_leaves_the_skipped_pr_skipped(label):
    """Every label edit fires the same event, so admitting them all would start
    a job per label a maintainer adds. The reviewer's own decide script declines
    a `labeled` event that is not the opt-in one, so a read is never spent — but
    the caller does not even start for a PR its skip set excludes."""
    pl = payload(
        action="labeled",
        title="chore: bump a pin",
        association="OWNER",
        same_repo=True,
        label=label,
    )
    assert not reviews(pl)


@pytest.mark.parametrize("title", SKIP_TITLES + REVIEWED_TITLES)
@pytest.mark.parametrize("association", UNTRUSTED)
def test_an_untrusted_author_never_buys_the_stand_in_note(title, association):
    """THE fix. A fork PR's title is attacker-chosen, so no title may buy the
    stand-in note in place of a real read."""
    pl = payload(title=title, association=association, same_repo=False)
    assert not notes(pl), f"{association} bought the note with {title!r}"


@pytest.mark.parametrize("title", SKIP_TITLES + REVIEWED_TITLES)
@pytest.mark.parametrize("association", UNTRUSTED)
def test_an_untrusted_author_always_gets_the_real_review(title, association):
    """The other half, and the reason the guard cannot live on the note alone:
    guarding only the note leaves the same PR skipped by `review` and noted by
    nobody, so the findings gate holds it with no event able to clear it."""
    pl = payload(title=title, association=association, same_repo=False)
    assert reviews(pl), f"{association}'s {title!r} PR is reviewed by nobody"


@pytest.mark.parametrize("title", SKIP_TITLES + REVIEWED_TITLES)
@pytest.mark.parametrize("association", UNTRUSTED + TRUSTED)
@pytest.mark.parametrize("same_repo", [True, False])
def test_every_pull_request_is_either_reviewed_or_noted(title, association, same_repo):
    """The stranding invariant, over the whole payload space: a PR the reviewer
    skips must be one the note picks up, and vice versa. Exactly one of the two
    jobs claims each `opened` PR."""
    pl = payload(title=title, association=association, same_repo=same_repo)
    assert reviews(pl) != notes(pl), (
        f"{association}/{same_repo}/{title!r} is claimed by "
        f"{'both' if reviews(pl) else 'neither'} job"
    )


@pytest.mark.parametrize("title", SKIP_TITLES)
@pytest.mark.parametrize("association", TRUSTED)
def test_a_trusted_author_keeps_the_low_risk_skip(title, association):
    """The feature the guard must not eat: a maintainer's chore/style/release PR
    still skips the model spend and still collects its note."""
    pl = payload(title=title, association=association, same_repo=True)
    assert not reviews(pl)
    assert notes(pl)


@pytest.mark.parametrize("title", SKIP_TITLES)
def test_a_same_repo_branch_is_trusted_on_its_own(title):
    """A branch in this repo means the author already has push access, so the
    skip holds even when author_association reads NONE (as it does for a PR
    opened by an app on a same-repo branch)."""
    pl = payload(title=title, association="NONE", same_repo=True)
    assert not reviews(pl)
    assert notes(pl)


def test_a_bot_pull_request_is_skipped_and_noted_from_a_fork_too():
    """`user.type` is a payload fact both jobs read the same way, so the bot
    class agrees without the trust guard — pinned so a future edit that guards
    one arm and not the other cannot strand a Dependabot PR."""
    pl = payload(title="chore(deps): bump x", bot=True, same_repo=False)
    assert not reviews(pl)
    assert notes(pl)


def test_a_push_still_notes_a_skipped_pr_that_missed_its_opened_window():
    """Under `pull_request_target` the OPENED event runs the BASE's workflow file,
    so a skipped PR opened before this job existed on the default branch got no
    note and could never satisfy the gate's reviewed-at-all leg. #30 stranded that
    way with auto-merge armed. `synchronize` is the recovery, and the script
    no-ops when the reviewer already has a review."""
    pl = payload(action="synchronize", title="chore: x", bot=True)
    assert notes(pl)
    assert not reviews(pl)


def test_a_push_does_not_note_a_pr_the_reviewer_actually_reads():
    # A reviewed PR clears the leg with a real review; the note is for the skipped
    # classes only, whatever event delivers it.
    pl = payload(action="synchronize", title="feat: x", same_repo=True)
    assert not notes(pl)


def test_a_draft_is_neither_reviewed_nor_noted():
    pl = payload(title="feat: x", draft=True, same_repo=True)
    assert not reviews(pl)
    assert not notes(pl)


# ── Non-vacuity: the same invariants must REJECT the pre-fix conditions ──────


@pytest.mark.parametrize("title", SKIP_TITLES)
def test_the_pre_fix_conditions_fail_the_fork_guard_invariant(title):
    """If these ever pass, the invariants above stopped testing the fix."""
    pl = payload(title=title, association="NONE", same_repo=False)
    assert evaluate(PRE_FIX_NOTE, pl), (
        "the pre-fix skip-set condition must accept an untrusted chore: PR — "
        "that was the bug"
    )
    assert not evaluate(PRE_FIX_DECIDE, pl), (
        "the pre-fix decide condition must skip the same PR — that was the bug"
    )


# ── the note is idempotent, because it now also runs on `synchronize` ─────────

NOTE_SCRIPT = REPO_ROOT / ".github" / "scripts" / "note-skipped-review.sh"

_FAKE_GH_FOR_NOTE = r"""#!/usr/bin/env python3
# gh stub for note-skipped-review.sh: answers the shared reviewer-reviews read
# from GH_REVIEWS (an NDJSON file, empty for "never reviewed") and records every
# review POST so a test can assert the note was or was not posted.
import os, sys

args = sys.argv[1:]
if args[:2] == ["api", "graphql"]:
    sys.stdout.write(open(os.environ["GH_REVIEWS"], encoding="utf-8").read())
    sys.exit(0)

if args[0] == "api" and "-X" in args and args[args.index("-X") + 1] == "POST":
    with open(os.environ["POST_LOG"], "a", encoding="utf-8") as f:
        f.write("posted\n")
    sys.exit(0)

sys.stderr.write("fake gh: unhandled %r\n" % (sys.argv,))
sys.exit(2)
"""


def _run_note(tmp_path, *, already_reviewed: bool) -> tuple[int, str, int]:
    """Drive the REAL script; return its status, its stdout and the POST count."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH_FOR_NOTE, encoding="utf-8")
    gh.chmod(0o755)
    reviews = tmp_path / "reviews.ndjson"
    reviews.write_text(
        '{"state":"COMMENTED"}\n' if already_reviewed else "", encoding="utf-8"
    )
    post_log = tmp_path / "posts.txt"
    post_log.touch()
    done = subprocess.run(
        ["bash", str(NOTE_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_TOKEN": "t",
            "GH_REPO": "o/r",
            "PR": "8",
            "GH_REVIEWS": str(reviews),
            "POST_LOG": str(post_log),
            "RETRY_BASE_DELAY": "0",
        },
    )
    posts = len(post_log.read_text(encoding="utf-8").split())
    return done.returncode, done.stdout + done.stderr, posts


def test_the_note_posts_when_the_reviewer_has_never_reviewed(tmp_path) -> None:
    rc, out, posts = _run_note(tmp_path, already_reviewed=False)
    assert rc == 0, out
    assert posts == 1, out


def test_the_note_posts_nothing_when_a_review_already_exists(tmp_path) -> None:
    # `synchronize` fires this job on every push, so without the guard a skipped
    # PR would collect one note per push.
    rc, out, posts = _run_note(tmp_path, already_reviewed=True)
    assert rc == 0, out
    assert posts == 0, out
    assert "posting no second note" in out
