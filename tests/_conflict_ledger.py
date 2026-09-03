"""The conflict ledger module, loaded once with its `_paths` import resolved.

`.github/resolver/auto-resolve/_paths.py` is the sibling module that classifies
a path. It is written in parallel with the ledger, so a stand-in stands in for
it while that file is absent, and the real module is imported the moment it
lands. The install happens HERE, not in each importer: the ledger's tests and
the FSM model that generates `docs/tla/ConflictLedger.tla` both need it, and two
copies of a stand-in can disagree about what `classify` answers.
"""

import sys
import types
from dataclasses import dataclass
from enum import StrEnum

from tests._resolver_helpers import REPO_ROOT, load_script, load_script_module

PATHS_PY = REPO_ROOT / ".github" / "resolver" / "auto-resolve" / "_paths.py"


def _stub_paths() -> types.ModuleType:
    """A stand-in for `_paths.py`, licensed because that file does not exist in
    this worktree yet — a parallel session is writing it.

    It answers the shape `_conflict_set` codes against and nothing more, so no
    test asserts on what it decides — only on what the ledger does with the
    answer.
    """
    module = types.ModuleType("_paths")

    class MergePolicy(StrEnum):
        PLAIN = "plain"
        DRIVER = "driver"
        UNMERGEABLE = "unmergeable"

    @dataclass(frozen=True, kw_only=True, slots=True)
    class PathFacts:
        path: str
        policy: MergePolicy
        binary: bool
        unmergeable: bool
        protected: bool
        harness_unwritable: bool
        generated_owned: bool
        lockfile: bool
        structural_unsafe: bool

    def classify(paths, *, base_remote_ref, owned):
        del base_remote_ref
        return {
            path: PathFacts(
                path=path,
                policy=MergePolicy.PLAIN,
                binary=False,
                unmergeable=False,
                protected=False,
                harness_unwritable=False,
                generated_owned=path in owned,
                lockfile=path.endswith(".lock"),
                structural_unsafe=False,
            )
            for path in paths
        }

    module.MergePolicy = MergePolicy
    module.PathFacts = PathFacts
    module.classify = classify
    return module


if "_paths" not in sys.modules:
    sys.modules["_paths"] = (
        load_script_module("_paths", PATHS_PY) if PATHS_PY.exists() else _stub_paths()
    )

paths = sys.modules["_paths"]
conflict_set = load_script(".github/resolver/auto-resolve/_conflict_set.py")
