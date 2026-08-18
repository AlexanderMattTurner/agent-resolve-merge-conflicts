"""The pinned tla2tools.jar, installed and digest-verified on demand.

`checks/tla-model-check.py` runs TLC out of this jar. It asks here rather than
resolving the installer's path itself, so a change to the installer's contract
reaches every caller.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resolver"))
# pylint: disable=wrong-import-position  # must follow the sys.path inserts above
from _loud_run import run_loud  # noqa: E402  (path inserted just above)

from repolint._root import repo_root  # noqa: E402  (path inserted just above)

INSTALLER = repo_root(Path(__file__)) / ".github/scripts/install-tla2tools.sh"


def resolve_jar() -> str:
    """The pinned jar's path. The installer verifies the digest and caches, so
    this costs one `sha256sum` per run after the first.

    `run_loud` is what puts the installer's stderr in the failure: a bare
    `CalledProcessError` reports only `exit status 22`, which names curl's
    HTTP-error status and not the `503` from the release CDN that caused it."""
    return run_loud(["bash", str(INSTALLER)])
