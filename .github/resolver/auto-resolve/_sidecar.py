"""The scratch route: a model pass edits a copy outside the tree, and the
resolver installs it.

PROBLEM CLASS — a model edit that must reach the tree without the model writing
the tree. The Claude Code harness refuses a headless write under `.claude/` or to
the hook config, so the conflict fan-out delivers such a path (lib.sh
`harness_unwritable_matches`) this way, and every repair pass delivers every
path this way. This module holds what they share: the name of the record that
maps each path to its scratch file, the reader of that record, and the install
with its refusals.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _refusal import fail  # noqa: E402,I001  # pylint: disable=wrong-import-position

#: The record a model run writes into its log dir: {repository path: scratch path}.
RESOLUTIONS_FILE = "sidecar-resolutions.json"


def read_resolutions(path: Path) -> dict[str, str]:
    """The {repository path: scratch path} record at PATH, or {} when it is
    absent or unreadable. Each caller decides what a path missing from it means."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {}
    return document if isinstance(document, dict) else {}


def scratch_source(resolutions: dict[str, str], name: str) -> Path | None:
    """The scratch file RESOLUTIONS records for NAME, or None when it records none
    or the file holds no bytes. An empty file counts as none, because a shard
    that declines writes nothing, and zero bytes cannot be told apart from that."""
    resolved = resolutions.get(name)
    source = Path(resolved) if isinstance(resolved, str) and resolved else None
    if source is None or not source.is_file() or not source.stat().st_size:
        return None
    return source


def install_scratch_copy(name: str, source: Path, within: Path) -> None:
    """Write SOURCE's bytes over the repository path NAME.

    INVARIANT — these refusals are what keep the scratch route from copying any
    file into the repository. A model run can write the record that names
    SOURCE, so a link, or a file outside WITHIN (the run's own log dir), would
    make the install read whatever file the run chose."""
    if source.is_symlink():
        fail(
            f"the sidecar resolution for '{name}' is a symlink ('{source}')",
            f"the file the resolver handed out for `{name}` is a "
            "symbolic link rather than the merged content, so installing "
            "it would commit whatever it points at. Resolve this one by "
            "hand.",
        )
    if not source.resolve().is_relative_to(within.resolve()):
        fail(
            f"the sidecar resolution for '{name}' ('{source}') lies outside '{within}'",
            f"the file the resolver handed out for `{name}` is not in the "
            "run's own scratch directory, so installing it would commit "
            "whatever file the run named. Resolve this one by hand.",
        )
    Path(name).write_bytes(source.read_bytes())
