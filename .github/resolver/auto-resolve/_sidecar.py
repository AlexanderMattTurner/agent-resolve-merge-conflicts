"""The scratch route for a path the model may read but never write in place.

PROBLEM CLASS — a path the Claude Code harness refuses to let the model write
(lib.sh `harness_unwritable_matches`, such as anything under `.claude/`). A model
pass that must change such a path writes a scratch file outside the repository
instead, and the resolver installs that file into the tree. The conflict fan-out
and the repair pass both deliver this way. This module holds what they share: the
pattern that names such a path, the name of the record that maps each path to its
scratch file, the reader of that record, and the install with its refusal.
"""

import json
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _refusal import fail  # noqa: E402,I001  # pylint: disable=wrong-import-position

#: The record a model run writes into its log dir: {repository path: scratch path}.
RESOLUTIONS_FILE = "sidecar-resolutions.json"

_SHARED_NAMES = json.loads(
    (Path(__file__).resolve().parent.parent / "lib" / "shared-names.json").read_text(
        encoding="utf-8"
    )
)


def harness_unwritable(paths: Iterable[str]) -> list[str]:
    """The members of PATHS the harness refuses to let the model write, sorted.

    lib.sh `harness_unwritable_matches` answers the same question with the same
    default and the same override, AUTO_RESOLVE_HARNESS_UNWRITABLE_RE: unset
    means the shared-names.json default, and empty disables the class."""
    pattern = os.environ.get(
        "AUTO_RESOLVE_HARNESS_UNWRITABLE_RE",
        _SHARED_NAMES["auto_resolve"]["harness_unwritable_re"],
    )
    if not pattern:
        return []
    unwritable = re.compile(pattern)
    return sorted({path for path in paths if unwritable.search(path)})


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
            f"the sidecar resolution for '{name}' ('{source}') lies outside "
            f"'{within}'",
            f"the file the resolver handed out for `{name}` is not in the "
            "run's own scratch directory, so installing it would commit "
            "whatever file the run named. Resolve this one by hand.",
        )
    Path(name).write_bytes(source.read_bytes())
