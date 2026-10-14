"""Which merges of a generated or release-owned path the caller's own rules decide.

PROBLEM CLASS — a merge of a file nobody may merge. A repository that moves its
build output off the default branch deletes each artifact on the pull request's
side, while every release commit on the base rewrites the same artifact (a
checksum manifest, a bundled hook). Each such path conflicts, and its answer is
mechanical, so no model and no human should be asked for it:

- the pull request deleted the path and the base rewrote it: keep the deletion;
- a `release` path both sides rewrote: take the base's bytes whole, because a
  line merge of a checksum manifest is a manifest no release ever wrote;
- the base deleted the path and the pull request rewrote it: a real decision,
  so a human makes it.

The declaration is `resolve-generated.mjs --owned --deletion-wins`, one
`path<TAB>kind` per line from the TRUSTED base. A line with no tab is an older
resolver that ignored the flag and printed its plain owned list, so it declares
nothing. Anything this module does not decide reaches the resolver's other
passes unchanged.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: I001  # pylint: disable=wrong-import-position
    bind_repo,
    git,
    git_status,
)
from _owned import (  # pylint: disable=wrong-import-position
    Owned,
    parse as parse_owned,
)

KINDS = ("generated", "release")
_NOUN = {"generated": "generated", "release": "release-owned"}


@dataclass(frozen=True, kw_only=True, slots=True)
class Decision:
    """One path's answer: `delete`, `take-base` or `human`, and why."""

    path: str
    action: str
    reason: str


def parse(text: str) -> dict[str, Owned]:
    """The declaration as one ownership answer per kind.

    Raises on a tab-separated line naming an unknown kind: that is a contract
    break between the resolver and this reader, and skipping it would hand a
    release-owned path to a hand merge.
    """
    by_kind: dict[str, list[str]] = {kind: [] for kind in KINDS}
    for line in text.splitlines():
        if "\t" not in line:
            continue
        path, kind = (field.strip() for field in line.split("\t", 1))
        if kind not in by_kind:
            raise SystemExit(f"_deletion_wins.py: unknown kind {kind!r} for {path!r}")
        by_kind[kind].append(path)
    return {kind: parse_owned("\n".join(paths)) for kind, paths in by_kind.items()}


def _changed(since: str, ref: str) -> set[str]:
    return set(
        filter(
            None,
            git("diff", "--name-only", "-z", "--no-renames", since, ref).split("\0"),
        )
    )


def _exists(ref: str, path: str) -> bool:
    return git_status("cat-file", "-e", f"{ref}:{path}") == 0


def decide(declared: dict[str, Owned], head: str, base: str) -> list[Decision]:
    """Every declared path BOTH sides changed since their merge base, decided.

    "Both changed", never "git left it conflicted": git line-merges a manifest
    whose two sides touched different lines without a conflict, and that merge
    is still a manifest no release produced.
    """
    merge_base = git("merge-base", head, base).strip()
    decisions = []
    for path in sorted(_changed(merge_base, head) & _changed(merge_base, base)):
        kinds = [kind for kind in KINDS if declared[kind].covers(path)]
        if not kinds:
            continue
        on_head, on_base = _exists(head, path), _exists(base, path)
        if len(kinds) > 1:
            # resolve-generated.mjs refuses such a table; an older one may not.
            decisions.append(
                Decision(
                    path=path,
                    action="human",
                    reason="it is declared both generated and release-owned, so its ownership is ambiguous",
                )
            )
        elif on_base and not on_head:
            decisions.append(
                Decision(
                    path=path,
                    action="delete",
                    reason=f"this pull request deleted this {_NOUN[kinds[0]]} file and the base only rewrote it",
                )
            )
        elif on_head and not on_base:
            decisions.append(
                Decision(
                    path=path,
                    action="human",
                    reason=f"the base deleted this {_NOUN[kinds[0]]} file and this pull request rewrote it, so keeping or dropping it is a decision",
                )
            )
        elif on_head and kinds == ["release"]:
            decisions.append(
                Decision(
                    path=path,
                    action="take-base",
                    reason="both sides rewrote this release-owned file, and only the base's copy is one a release wrote",
                )
            )
    return decisions


def main() -> None:
    """One NUL-terminated `path`, `action` then `reason` record per decided path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--declared", required=True, help="--deletion-wins output")
    parser.add_argument("--head", required=True, help="the pull request's side")
    parser.add_argument("--base", required=True, help="the side merged into it")
    args = parser.parse_args()

    bind_repo(args.root)
    declared = parse(Path(args.declared).read_text(encoding="utf-8"))
    sys.stdout.write(
        "".join(
            f"{d.path}\0{d.action}\0{d.reason}\0"
            for d in decide(declared, args.head, args.base)
        )
    )


if __name__ == "__main__":
    main()
