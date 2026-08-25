#!/usr/bin/env python3
"""Print the resolver ref `auto-resolve-conflicts.yaml` names on its `uses:`.

PROBLEM CLASS — one resolver version named by several readers, kept in step by
nothing. The resolver's code runs beside this repository's tokens, so which
version it is IS the security control. A clone that names no ref takes the
remote's HEAD, so a consumer runs upstream code it never accepted.

GitHub interpolates nothing into a `uses:`, so a workflow line cannot read a
shared value. That makes the caller's `uses:` the source, and every other reader
asks here.

Standard library plus PyYAML, on the system interpreter — these jobs install no
venv.
"""

import sys
from pathlib import Path

import yaml

CALLER = Path(".github/workflows/auto-resolve-conflicts.yaml")
JOB = "resolve"


def resolver_ref(root: Path) -> str:
    """The git ref the `resolve` job's `uses:` names.

    INVARIANT: what this returns is safe to pass as a `git fetch` argument. A
    caller interpolates it straight into one, and git's option parser does not
    stop at the remote name, so a `--upload-pack=…` ref would be read as an
    option.
    """
    doc = yaml.safe_load((root / CALLER).read_text(encoding="utf-8"))
    uses = doc["jobs"][JOB]["uses"]
    target, sep, ref = uses.partition("@")
    if not sep or not ref or ref.startswith("-"):
        raise SystemExit(
            f"{CALLER}'s `{JOB}` job calls {target!r} with no usable `@ref` — the "
            "resolver version must be named on the `uses:` line, and never with a "
            "leading `-`, which a `git fetch` would read as an option."
        )
    return ref


if __name__ == "__main__":
    print(resolver_ref(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
