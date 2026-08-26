"""The `@claude` responder in claude.yaml fires on four event kinds and holds
contents/pull-requests/issues write. Its `issues` arm additionally requires a
HUMAN author (`github.event.issue.user.type == 'User'`): an issue a GitHub App
files can carry text the app merely relayed from somewhere else, so a bot-filed
issue whose body says `@claude` must NOT start the responder.

The three comment arms are gated by claude-code-action's own write-access check
on the COMMENTER, so they must not inherit the author clause — a human replying
under a bot-filed issue still reaches the responder.

These tests EVALUATE the job's real `if:` expression against synthetic payloads
rather than grepping the file, so a clause present but wired into the wrong arm
still reds.
"""

from functools import lru_cache

import pytest
import yaml

from tests._gha_if import evaluate
from tests._helpers import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude.yaml"

MENTION = "please help @claude"
NO_MENTION = "please help someone else"

# Which payload field carries the mention text for each event the workflow
# subscribes to. The last test proves this covers every entry under `on:`.
MENTION_FIELD = {
    "issues": "issue_body",
    "issue_comment": "comment_body",
    "pull_request_review_comment": "comment_body",
    "pull_request_review": "review_body",
}
COMMENT_EVENTS = [
    (name, field) for name, field in MENTION_FIELD.items() if name != "issues"
]


@lru_cache(maxsize=None)
def _claude_condition() -> str:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    condition = jobs["claude"].get("if")
    assert condition, "the claude job lost its `if:` guard"
    return condition


def payload(
    *,
    event_name: str,
    comment_body: str = NO_MENTION,
    review_body: str = NO_MENTION,
    issue_body: str = NO_MENTION,
    issue_title: str = NO_MENTION,
    issue_author_type: str = "User",
) -> dict:
    return {
        "github": {
            "event_name": event_name,
            "event": {
                "comment": {"body": comment_body},
                "review": {"body": review_body},
                "issue": {
                    "body": issue_body,
                    "title": issue_title,
                    "user": {"type": issue_author_type},
                },
            },
        }
    }


def responds(pl: dict) -> bool:
    return evaluate(_claude_condition(), pl)


# ── The `issues` arm: a human author is required ─────────────────────────────


@pytest.mark.parametrize("field", ["issue_body", "issue_title"])
@pytest.mark.parametrize("author_type", ["Bot", "Organization", "Mannequin"])
def test_a_non_human_issue_author_cannot_trigger_the_responder(field, author_type):
    """The guard. RED without `github.event.issue.user.type == 'User'`: a bot that
    relays outside text into an issue starts an agent holding write access."""
    pl = payload(event_name="issues", issue_author_type=author_type, **{field: MENTION})
    assert not responds(pl)


@pytest.mark.parametrize("field", ["issue_body", "issue_title"])
def test_a_human_filed_issue_still_reaches_the_responder(field):
    """The feature the guard must not eat: a person opening an issue that says
    `@claude` still gets an answer, from the body or from the title."""
    pl = payload(event_name="issues", issue_author_type="User", **{field: MENTION})
    assert responds(pl)


def test_a_human_issue_without_a_mention_is_ignored():
    """`user.type == 'User'` gates the arm; it must not become the whole arm."""
    assert not responds(payload(event_name="issues", issue_author_type="User"))


# ── The other three arms: the author clause must not leak into them ──────────


@pytest.mark.parametrize(("event_name", "field"), COMMENT_EVENTS)
@pytest.mark.parametrize("issue_author_type", ["User", "Bot"])
def test_a_comment_arm_does_not_inherit_the_issue_author_clause(
    event_name, field, issue_author_type
):
    """A comment arm reads the COMMENTER, whom claude-code-action checks for write
    access. Hoisting `user.type` out of the `issues` parenthesis would strand a
    human's `@claude` reply under any bot-filed issue or bot-opened PR."""
    pl = payload(
        event_name=event_name,
        issue_author_type=issue_author_type,
        **{field: MENTION},
    )
    assert responds(pl)


@pytest.mark.parametrize(("event_name", "field"), COMMENT_EVENTS)
def test_a_comment_arm_still_needs_the_mention(event_name, field):
    """Each comment arm keeps the mention gate it was always built on."""
    assert not responds(payload(event_name=event_name, **{field: NO_MENTION}))


def test_every_subscribed_event_has_an_arm_that_can_fire():
    """An `on:` entry with no matching arm starts a run that immediately skips, so
    a new event kind must be wired into the condition deliberately."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # YAML 1.1 reads a bare `on` key as the boolean True.
    triggers = set(workflow.get("on", workflow.get(True)))
    assert triggers == set(MENTION_FIELD), (
        f"claude.yaml subscribes to {sorted(triggers)}; this test models "
        f"{sorted(MENTION_FIELD)}"
    )
    for event_name, field in MENTION_FIELD.items():
        assert responds(payload(event_name=event_name, **{field: MENTION})), (
            f"no `if:` arm fires for the {event_name} event"
        )
