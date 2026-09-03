"""The conflict ledger module, loaded once with its `_paths` import resolved.

The ledger's own tests and the FSM model that generates
`docs/tla/ConflictLedger.tla` both need it, and `load_script` builds a SEPARATE
module object per call. Loading `_paths` twice would give the ledger one
`MergePolicy` and its caller another, so an `is` comparison between two members
spelled alike would be false. The install happens here, once.
"""

import sys

from tests._resolver_helpers import REPO_ROOT, load_script, load_script_module

PATHS_PY = REPO_ROOT / ".github" / "resolver" / "auto-resolve" / "_paths.py"

if "_paths" not in sys.modules:
    sys.modules["_paths"] = load_script_module("_paths", PATHS_PY)

paths = sys.modules["_paths"]
conflict_set = load_script(".github/resolver/auto-resolve/_conflict_set.py")
