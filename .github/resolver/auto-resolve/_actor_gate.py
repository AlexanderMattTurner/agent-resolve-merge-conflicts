"""The gate on WHO a paid fan-out runs for, spent before any shard starts.

Fail-CLOSED and whitelist-only: a bot in BOT_ACTORS, or an actor the API
affirmatively reports as admin/write. claude-code-action refuses every other
actor itself, so a run for one would spend a resolution and land nothing.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
from _ci_retry import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    with_retry,
)
from _exit_codes import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    EXIT_MISCONFIGURED,
)
from _fanout_report import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    die,
)

# The bots this resolver admits: the relay dispatch that carries a
# push-discovered conflict into a workflow_dispatch, and any app a caller adds
# through the workflow's `bot-actors` input. Neither is a collaborator, so the
# probe below 404s for both and would deny an actor the sibling gate admits.
# The gate stays fail-closed and whitelist-only, so an input naming nothing
# admits no bot rather than any.
BOT_ACTORS = tuple(
    os.environ.get("AUTO_RESOLVE_BOT_ACTORS", "github-actions")
    .replace(",", " ")
    .split()
)


def retry_stdout(*command: str) -> str:
    """The shared exponential-backoff retry (_ci_retry), for a capture. Only
    the SUCCEEDING attempt's stdout is returned — `gh api` prints the HTTP error
    body on stdout too, so concatenating attempts would hand the caller that
    garbage alongside the eventual answer. An exhausted retry answers "", read
    as "never answered", not a value."""

    def once() -> subprocess.CompletedProcess:
        done = subprocess.run(command, capture_output=True, text=True, check=False)
        sys.stderr.write(done.stderr)
        return done

    done = with_retry(" ".join(command), once, lambda: None)
    return done.stdout.strip("\n") if done is not None else ""


def assert_actor_allowed(actor: str, repo: str) -> None:
    """Refuse to spend on a run whose actor claude-code-action would itself
    refuse. Fail-CLOSED and whitelist-only: a bot in BOT_ACTORS, or an actor
    the API affirmatively reports as admin/write."""
    if not actor:
        die(
            "no TRIGGERING_ACTOR — cannot verify the run's initiator; "
            "refusing to spend.",
            EXIT_MISCONFIGURED,
        )
    if actor.removesuffix("[bot]") in BOT_ACTORS:
        return
    # Idempotent GET, so a transient 5xx is worth riding out rather than
    # denying a maintainer on a claim never established.
    permission = retry_stdout(
        "gh",
        "api",
        f"repos/{repo}/collaborators/{actor}/permission",
        "--jq",
        ".permission",
    )
    # Whitelist-only: no novel value reads as a pass.
    if permission in ("admin", "write"):
        return
    shown_repo = repo or "<unset>"
    if not permission:
        die(
            f"could not establish whether '{actor}' has write access to "
            f"{shown_repo} — the permission probe returned nothing after retries. "
            "Refusing to spend rather than assuming either answer.",
            EXIT_MISCONFIGURED,
        )
    die(
        f"actor '{actor}' has no write access to {shown_repo} (probe returned "
        f"'{permission}') — refusing to run a paid conflict resolution for an "
        "actor claude-code-action would reject.",
        EXIT_MISCONFIGURED,
    )
