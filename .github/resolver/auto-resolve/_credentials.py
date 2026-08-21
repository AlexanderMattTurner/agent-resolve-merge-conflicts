"""Which credential each rung of the OAuth ladder authenticates through.

One definition, because two readers walk the same ladder — the repair pass here
and `self_review.py` — and a rung authenticated through the wrong env var runs
as an unauthenticated one, which reads as the model failing rather than as a
misconfigured job.
"""

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

_OAUTH_LADDER_LIB = _SCRIPT_DIR.parent / "lib" / "oauth-ladder.bash"


def oauth_ladder_names() -> list[str]:
    """The variable names holding this job's credentials, in attempt order.

    Runs the tree's one ladder walk rather than repeating it, so this step and
    the shell steps beside it can never disagree about which rung a run spends.
    It returns NAMES: the values stay in this process's own environment instead
    of crossing a pipe into a buffer a traceback could print. A walk that cannot
    run raises, because an empty list here reads as "no credential is configured"
    and would silently skip the self-review gate — the gate failing OPEN, which
    is the silent degradation to an unreviewed bundle this step exists to prevent.
    """
    done = subprocess.run(
        ["bash", "-c", f'source "{_OAUTH_LADDER_LIB}"; oauth_ladder_names'],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.split()


def _is_metered_credential(token: str) -> bool:
    """Whether TOKEN is the ladder's metered Anthropic API key rather than a
    subscription OAuth token — `oauth_ladder_is_metered`, the one shape test
    every ladder walker shares, so this process and `self_review.py` agree on
    which env var (`ANTHROPIC_API_KEY` vs `CLAUDE_CODE_OAUTH_TOKEN`) a rung
    authenticates through."""
    done = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{_OAUTH_LADDER_LIB}"; oauth_ladder_is_metered "$1"',
            "_",
            token,
        ],
        check=False,
    )
    return done.returncode == 0


def _claude_cli_env_for(token: str) -> dict[str, str]:
    """The `claude` CLI env var TOKEN authenticates through, with the other one
    forced empty so a stale value from an earlier rung, or the job's own env,
    cannot leak into this rung's run."""
    if _is_metered_credential(token):
        return {"CLAUDE_CODE_OAUTH_TOKEN": "", "ANTHROPIC_API_KEY": token}
    return {"CLAUDE_CODE_OAUTH_TOKEN": token, "ANTHROPIC_API_KEY": ""}


def ordered_oauth_tokens() -> list[str]:
    """Configured credentials, with the resolver's proven credential first."""
    tokens: list[str] = []
    for value in (
        os.environ.get("RESOLVER_PREFERRED_TOKEN"),
        *(os.environ.get(name) for name in oauth_ladder_names()),
    ):
        if value and value not in tokens:
            tokens.append(value)
    return tokens
