"""Which credential each rung of the OAuth ladder authenticates through.

One definition, because two readers walk the same ladder — the repair pass here
and `self_review.py` — and a rung authenticated through the wrong env var runs
as an unauthenticated one, which reads as the model failing rather than as a
misconfigured job.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

_OAUTH_LADDER_LIB = _SCRIPT_DIR.parent / "lib" / "oauth-ladder.bash"


def oauth_ladder_var_names() -> list[str]:
    """EVERY variable name the ladder can hold a credential in, configured or not.

    The order is the ladder's attempt order, and an empty or repeated rung is
    dropped by the reader — `lib/oauth-ladder.bash` states the same two rules for
    the shell callers, over this same list.
    """
    names = json.loads(
        (_SCRIPT_DIR.parent / "lib" / "shared-names.json").read_text(encoding="utf-8")
    )["oauth_ladder_vars"]
    return list(names)


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


#: Where the resolve job puts the credential its own fan-out proved, as a VALUE
#: under a name the ladder list does not carry. `auto-resolve.yaml` writes it.
_PREFERRED = "RESOLVER_PREFERRED_TOKEN"

#: What `withhold_from_children` took out of `os.environ`, by name. Merged back
#: over the environment on every read below, never cached as an ANSWER: a cached
#: one is wrong for the first read that happens before the credentials arrive.
_WITHHELD: dict[str, str] = {}


def _reset_process_state() -> None:
    """Forget what an earlier run took, so a long-lived worker importing this
    module once cannot hand one run's credentials to the next."""
    _WITHHELD.clear()


def withhold_from_children() -> None:
    """Move this job's credentials out of the environment and into THIS process.

    PROBLEM CLASS — a credential every child inherits. The resolve job runs the
    MERGED TREE's own scripts: the caller's pre-pass, its setup command, its
    post-merge check and its pre-commit hooks. Each is spawned with no `env=` of
    its own, so each inherited every rung, and filtering at ONE spawn leaves the
    others — the post-merge check was already one it left. Taking the names once,
    before anything spawns, covers every spawn including the next one added, and
    no indirection inside those scripts reaches past a variable that is not there.

    The model calls are unaffected: each builds its child's environment from the
    token it holds — `_claude_cli_env_for` here, `_claude_env` in self_review.py —
    so the credential reaches `claude` and nothing else.
    """
    for name in (_PREFERRED, *oauth_ladder_var_names()):
        value = os.environ.pop(name, "")
        if value:
            _WITHHELD[name] = value


def ordered_oauth_tokens() -> list[str]:
    """Configured credentials, with the resolver's proven credential first.

    Reads what `withhold_from_children` took ALONGSIDE the environment, so the
    answer is the same before and after it runs.
    """
    held = {**os.environ, **_WITHHELD}
    tokens: list[str] = []
    for value in (
        held.get(_PREFERRED),
        *(held.get(name) for name in oauth_ladder_var_names()),
    ):
        if value and value not in tokens:
            tokens.append(value)
    return tokens
