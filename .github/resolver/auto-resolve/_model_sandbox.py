"""What a `claude` child process may hold and touch.

PROBLEM CLASS — a model process that inherits a GitHub write token or a runner
command file. The model reads conflict text and merge deltas a fork author or an
outsider's merged change wrote, so an instruction in that text must find no
token to spend and no file that changes a later step. One definition, because
the fan-out shards, the hook-repair pass and the self-review each spawn `claude`.
"""

import json
from pathlib import Path

# Absolute: the CLI resolves a hook command against the WORKSPACE, not this
# script's directory.
_HOOK = Path(__file__).resolve().parent / "shard-permission.mjs"

#: Every name a GitHub token travels under, and every runner file a step writes to
#: change what a LATER step runs with. `gh` reads the first two token names, and the
#: enterprise pair is its spelling for a GHES host.
WITHHELD_FROM_MODEL = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_ENV",
        "GITHUB_PATH",
        "GITHUB_OUTPUT",
        "GITHUB_STATE",
        "GITHUB_STEP_SUMMARY",
    }
)


def model_environment(env: dict[str, str]) -> dict[str, str]:
    """ENV without the names above: the base of every `claude` child's environment."""
    return {key: value for key, value in env.items() if key not in WITHHELD_FROM_MODEL}


def write_permission_settings(config_dir: Path) -> None:
    """The user settings a run's CLI loads, wiring the PreToolUse hook that lets a
    run write exactly its granted paths and read no kernel interface. See
    shard-permission.mjs for the grants it reads from the environment."""
    config_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": f"node {_HOOK}"}],
                }
            ]
        }
    }
    (config_dir / "settings.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8"
    )
