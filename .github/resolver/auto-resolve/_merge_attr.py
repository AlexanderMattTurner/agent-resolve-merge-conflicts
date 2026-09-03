"""What `.gitattributes` says about merging a path, decided in ONE place.

PROBLEM CLASS — `git check-attr merge` read with a different acceptance set at
each call site. A path whose attribute named a driver other than mergiraf was
mergeable to prepare.sh's partition, unportable to the relocation port, and
un-narrowable to the table pre-pass: three verdicts about one path, from three
readers of one command. `merge=binary` was a fourth disagreement — the relocation
port refuses it and prepare.sh called it an ordinary text conflict.

The semantics are pure functions here, so a caller keeps whatever git plumbing it
already has and takes only the answer. `policies` is the batched form for the
callers that reach git through `_git_io`.
"""

import argparse
import fnmatch
import json
import sys
from enum import StrEnum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    bind_repo,
    git,
)

_SHARED_NAMES = json.loads(
    (Path(__file__).resolve().parent.parent / "lib" / "shared-names.json").read_text(
        encoding="utf-8"
    )
)

#: git leaves the conflict for a human and writes NO markers, so no textual
#: resolution exists. `-merge` reads as `unset`; the built-in `binary` driver
#: means the same thing by name.
UNMERGEABLE_MERGE_ATTRS = frozenset({"unset", "binary"})
#: The answers meaning "no merge policy is configured", so a caller may run the
#: built-in line merge itself. `git merge-file` dispatches on no attribute and no
#: driver, so anything outside this set is a policy it would silently ignore.
PLAIN_MERGE_ATTRS = frozenset({"unspecified", "set", "text"})
#: git's third built-in driver. It has no `merge.union.driver` to look up, so a
#: config lookup misses it and only `git merge-file --union` performs it.
UNION_MERGE_ATTR = "union"
#: The syntax-aware driver install-mergiraf.sh binds in the resolver's checkout.
STRUCTURAL_DRIVER = "mergiraf"
#: The file types that driver DROPS content on, so the resolver unbinds them.
#: lib.sh reads the same list for the `$GIT_DIR/info/attributes` it writes.
STRUCTURAL_SKIP_GLOBS = tuple(_SHARED_NAMES["auto_resolve"]["structural_skip_globs"])


class MergePolicy(StrEnum):
    """How a path's merge is settled, as the whole resolver reads it."""

    PLAIN = "plain"
    """The built-in line merge. Any pass may run it."""
    DRIVER = "driver"
    """A named merge driver the repository configured. Git applies it during the
    merge, so a pass that re-merges the path itself would drop it silently."""
    UNMERGEABLE = "unmergeable"
    """No markers and no textual resolution. Only a human can settle it."""


def attr_value(line: str) -> str:
    """The value out of one `git check-attr` line, which reads `PATH: merge: V`."""
    return line.rsplit(": ", 1)[-1].strip()


def effective_attr(attr: str, default: str) -> str:
    """ATTR with `unspecified` resolved as git resolves it.

    gitattributes(5): an unspecified `merge` takes the `merge.default` driver, so
    a repository that binds one repo-wide and writes no per-path line still gets
    that driver. An unbound default falls through to the built-in text merge.
    """
    if attr != "unspecified":
        return attr
    return default or "text"


def structurally_unsafe(path: str) -> bool:
    """Whether the syntax-aware driver drops content on PATH's file type.

    Case-INSENSITIVE, because `mergiraf solve` keys on the real filename while
    git's own globs are case-sensitive, so `Config.YAML` reaches the drop that
    `*.yaml` alone would miss.
    """
    name = path.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(name, glob) for glob in STRUCTURAL_SKIP_GLOBS)


def effective_driver(path: str, attr: str, default: str) -> str:
    """What THIS resolver merges PATH with, which is what `effective_attr` says
    unless the resolver has unbound the driver named there.

    `override_unsafe_merge_attributes` rebinds every structurally-unsafe path to
    the built-in text merge, and it does so by writing `$GIT_DIR/info/attributes`
    in the checkout that runs the merge. `git check-attr` reads that file at the
    TOP of the attribute stack, and no flag excludes it — `--source=<ref>`
    replaces only the in-tree `.gitattributes`. So the raw attribute answers
    `text` in the prepare job and `mergiraf` in every other job, for one path.
    Applying the same unbinding here makes the answer the checkout's, not the
    job's.
    """
    effective = effective_attr(attr, default)
    if effective == STRUCTURAL_DRIVER and structurally_unsafe(path):
        return "text"
    return effective


def policy_of(path: str, attr: str, default: str) -> MergePolicy:
    """One raw attribute as the policy every caller here reads."""
    effective = effective_driver(path, attr, default)
    if effective in UNMERGEABLE_MERGE_ATTRS:
        return MergePolicy.UNMERGEABLE
    if effective in PLAIN_MERGE_ATTRS:
        return MergePolicy.PLAIN
    return MergePolicy.DRIVER


def merge_default() -> str:
    """The driver `merge.default` binds, or the empty string when none is bound."""
    return git("config", "--get", "merge.default", check=False).strip()


def merge_attrs(paths: list[str], *, source: str | None = None) -> dict[str, str]:
    """Each PATH's raw `merge` attribute, as `git check-attr` spells it.

    SOURCE names the tree the in-tree `.gitattributes` is read from, and None
    reads the worktree. Mid-merge the worktree's copy is the pull request's own —
    or the marker-riddled file, when it conflicted too — and a branch can carry a
    `-merge` line the base has since removed, so a verdict taken from it is one
    the base already retracted.

    Batched with `-z`, which writes `<path> NUL <attribute> NUL <value> NUL`, so
    a path carrying a newline or a quote survives the read.
    """
    if not paths:
        return {}
    at = [f"--source={source}"] if source else []
    fields = git("check-attr", *at, "-z", "merge", "--", *paths).split("\0")[:-1]
    return dict(zip(fields[::3], fields[2::3], strict=True))


def policies(paths: list[str], *, source: str | None = None) -> dict[str, MergePolicy]:
    """Each PATH's merge policy, read from SOURCE's attributes."""
    default = merge_default()
    return {
        path: policy_of(path, attr, default)
        for path, attr in merge_attrs(paths, source=source).items()
    }


def bound_to_structural_driver(paths: list[str]) -> list[str]:
    """Which of PATHS git would merge with the syntax-aware driver right now.

    Read from the WORKTREE and not from a ref: the caller is about to write
    `$GIT_DIR/info/attributes` to unbind exactly these, and what it must unbind
    is what this checkout resolves today.
    """
    default = merge_default()
    return [
        path
        for path, attr in merge_attrs(paths).items()
        if effective_attr(attr, default) == STRUCTURAL_DRIVER
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bound-to-structural-driver",
        action="store_true",
        required=True,
        help="read NUL-separated paths on stdin, print the bound subset the same way",
    )
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    bind_repo(args.root)
    paths = [path for path in sys.stdin.buffer.read().decode().split("\0") if path]
    sys.stdout.write("".join(f"{path}\0" for path in bound_to_structural_driver(paths)))


if __name__ == "__main__":
    main()
