"""One entry per conflicted path, holding exactly one disposition.

PROBLEM CLASS — the state of a merge lives in about twenty parallel bash arrays,
so a path can sit in two partitions at once and nothing says which pass owns it.
This ledger is built ONCE from `git ls-files -u`, holds each path's three index
stages beside its classification, and records ONE disposition. A pass calls
`claim`; a second pass claiming the same path raises instead of quietly
disagreeing. `to_json` carries the whole set across a GitHub Actions step
boundary as one file, so a path whose name holds whitespace crosses whole where
a whitespace-joined step output has to drop it.
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _git_io import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    bind_repo,
)
from _owned import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    EMPTY,
    Owned,
    parse as parse_owned,
)
from _paths import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    MergePolicy,
    PathFacts,
    Shape,
    Stages,
    classify,
    unmerged_stages,
)


class ClaimConflict(Exception):
    """Raised when two passes claim one path, or a pass claims one it was not
    handed off."""


class UnclaimedPaths(Exception):
    """Raised when a driver asks to finish while some path has no disposition."""


class Claimed(StrEnum):
    """What a pass decided about a path."""

    UNCLAIMED = "unclaimed"
    STAGED = "staged"
    DEFERRED = "deferred"
    REFUSED = "refused"
    TO_MODEL = "to_model"


# A claim in one of these is the last word on a path: the pass either wrote the
# resolution, gave the path to a human, or spent the model on it.
_TERMINAL = frozenset({Claimed.STAGED, Claimed.REFUSED, Claimed.TO_MODEL})

# The prompt a TO_MODEL path is resolved under. One per shape the model can
# answer: marker text, a keep-or-delete verdict, and a file the resolver may
# read but not write.
PROMPTS = frozenset({"marker", "modify_delete", "sidecar"})

_STATE_FIELD = {
    Claimed.DEFERRED: "to",
    Claimed.REFUSED: "reason",
    Claimed.TO_MODEL: "prompt",
}


@dataclass(frozen=True, kw_only=True, slots=True)
class Disposition:
    """One pass's decision about one path.

    `by` names the pass that decided. `to` names the pass a DEFERRED path is
    handed to, `reason` says why a REFUSED path has no automated resolution,
    and `prompt` names which prompt resolves a TO_MODEL path.
    """

    claimed: Claimed
    by: str
    to: str = ""
    reason: str = ""
    prompt: str = ""

    def __post_init__(self) -> None:
        # INVARIANT — each of `to`, `reason` and `prompt` belongs to exactly one
        # state, and its state requires it. So no reader has to decide what a
        # `to` on a REFUSED entry meant, and no pass defers to nobody.
        owner = _STATE_FIELD.get(self.claimed)
        for name in ("to", "reason", "prompt"):
            value = getattr(self, name)
            if name == owner and not value:
                raise ValueError(f"a {self.claimed} claim needs a non-empty `{name}`")
            if name != owner and value:
                raise ValueError(f"a {self.claimed} claim holds no `{name}`: {value!r}")
        if self.claimed is Claimed.UNCLAIMED and self.by:
            raise ValueError(f"an unclaimed path names no pass: by={self.by!r}")
        if self.claimed is not Claimed.UNCLAIMED and not self.by:
            raise ValueError(f"a {self.claimed} claim names the pass that made it")
        if self.prompt and self.prompt not in PROMPTS:
            raise ValueError(
                f"unknown prompt {self.prompt!r}: one of {sorted(PROMPTS)}"
            )


@dataclass(frozen=True, kw_only=True, slots=True)
class Entry:
    """One conflicted path: what git recorded, what the path is, who owns it."""

    path: str
    stages: Stages
    facts: PathFacts
    disposition: Disposition


#: The pass every disposition below is made by, and the passes a deferral hands
#: a path to: `bundle` re-derives, `mergiraf` re-merges from the markers.
_BY = "prepare"
_BUNDLE = "bundle"
_MERGIRAF = "mergiraf"


def route(
    facts: PathFacts,
    *,
    lockfile_refused: bool,
    lockfile_deferred: bool,
    region_deferred: bool,
) -> Disposition:
    """Where prepare's partition sends FACTS' path.

    The keyword arguments carry what prepare knows and `classify` does not: the
    lockfile router's verdict for this path, and whether the generated-region
    pre-pass left the path for a later run.

    TOTAL — every `Shape`, every `MergePolicy` and every combination of the
    flags leaves by one of these returns, so a member added to either enum
    still gets an answer instead of reaching the end of a chain of tests.
    """
    # INVARIANT — the lockfile arms read the ROUTER's verdict, never
    # `facts.lockfile`. A recognized lockfile with no verdict is one the router
    # already regenerated or the caller's own rule owns, and both are settled.
    if lockfile_refused:
        return Disposition(
            claimed=Claimed.REFUSED,
            by=_BY,
            reason="no lock command available here regenerates this lockfile",
        )
    if lockfile_deferred or facts.generated_owned or region_deferred:
        return Disposition(claimed=Claimed.DEFERRED, by=_BY, to=_BUNDLE)
    if facts.unmergeable:
        return Disposition(
            claimed=Claimed.REFUSED,
            by=_BY,
            reason="no markers and no textual resolution: only a human settles it",
        )
    if facts.shape is Shape.MODIFY_DELETE:
        return Disposition(claimed=Claimed.TO_MODEL, by=_BY, prompt="modify_delete")
    return Disposition(claimed=Claimed.DEFERRED, by=_BY, to=_MERGIRAF)


class ConflictSet:
    """Every conflicted path in one merge, each with exactly one disposition."""

    def __init__(self, entries: dict[str, Entry]) -> None:
        self._entries = entries

    @classmethod
    def from_index(cls, *, base_remote_ref: str, owned: Owned = EMPTY) -> "ConflictSet":
        """The ledger git's index describes right now, every path UNCLAIMED.

        `base_remote_ref` and `owned` are `_paths.classify`'s own arguments: the
        tracking ref of the branch being merged in, and the paths the calling
        repository's rule table owns.
        """
        stages = unmerged_stages()
        facts = classify(sorted(stages), base_remote_ref=base_remote_ref, owned=owned)
        # INVARIANT — totality: every path git reports gets an entry. A path the
        # classifier answered nothing for would otherwise leave the set silently
        # short, and no pass would ever be asked to judge it.
        missing = sorted(set(stages) - set(facts))
        if missing:
            raise ValueError(f"_paths.classify returned no facts for {missing}")
        return cls(
            {
                path: Entry(
                    path=path,
                    stages=stage,
                    facts=facts[path],
                    disposition=Disposition(claimed=Claimed.UNCLAIMED, by=""),
                )
                for path, stage in stages.items()
            }
        )

    def entry(self, path: str) -> Entry:
        """PATH's entry. Raises when this merge left PATH unconflicted."""
        if path not in self._entries:
            raise KeyError(f"{path!r} is not a conflicted path in this merge")
        return self._entries[path]

    def entries(self) -> list[Entry]:
        """Every entry, sorted by path."""
        return [self._entries[path] for path in sorted(self._entries)]

    def partition(self, claimed: Claimed) -> list[str]:
        """The paths in CLAIMED, sorted."""
        return sorted(
            path
            for path, entry in self._entries.items()
            if entry.disposition.claimed is claimed
        )

    def claim(self, path: str, *, disposition: Disposition) -> None:
        """Record DISPOSITION about PATH, made by the pass its `by` names.

        Raises `ClaimConflict` when another pass already had the last word, or
        when a DEFERRED path is claimed by a pass other than the one it names.
        """
        by = disposition.by
        entry = self.entry(path)
        current = entry.disposition
        # INVARIANT — a terminal claim is final, and a deferral is finished only
        # by the pass it named. This refusal is what stops two passes silently
        # disagreeing about who owns a path.
        if current.claimed in _TERMINAL:
            raise ClaimConflict(
                f"{path}: {current.by!r} already claimed it {current.claimed}, "
                f"so {by!r} cannot claim it {disposition.claimed}"
            )
        if current.claimed is Claimed.DEFERRED and by != current.to:
            raise ClaimConflict(
                f"{path}: {current.by!r} deferred it to {current.to!r}, "
                f"so {by!r} cannot claim it"
            )
        self._entries[path] = replace(entry, disposition=disposition)

    def require_fully_dispositioned(self) -> None:
        """Raise unless every path carries a disposition.

        INVARIANT — the driver calls this before it writes its outputs. An
        UNCLAIMED path is a file whose bytes git wrote reaching the branch with
        no pass having judged them, which nothing downstream reports.
        """
        missing = self.partition(Claimed.UNCLAIMED)
        if missing:
            raise UnclaimedPaths(
                f"no pass judged {missing} — refusing to write outputs that would "
                "land the bytes git merged with nobody having read them"
            )

    def to_json(self) -> str:
        """The whole ledger as one JSON document.

        ASCII-escaped on purpose: this text crosses a step boundary as a file,
        and escaping keeps a path holding a newline or a non-ASCII character
        readable back whatever encoding the next step's locale gives it.
        """
        payload = {"entries": [asdict(entry) for entry in self.entries()]}
        return json.dumps(payload, ensure_ascii=True, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ConflictSet":
        """The ledger `to_json` wrote, with every enum back as an enum.

        The string a StrEnum member carries hashes as a plain string, so a
        decoded `"staged"` left as text would miss every set and identity test
        the claim rules make.
        """
        entries: dict[str, Entry] = {}
        for record in json.loads(text)["entries"]:
            facts = {
                **record["facts"],
                "policy": MergePolicy(record["facts"]["policy"]),
                "shape": Shape(record["facts"]["shape"]),
            }
            disposition = {
                **record["disposition"],
                "claimed": Claimed(record["disposition"]["claimed"]),
            }
            entries[record["path"]] = Entry(
                path=record["path"],
                stages=Stages(**record["stages"]),
                facts=PathFacts(**facts),
                disposition=Disposition(**disposition),
            )
        return cls(entries)


def _owned_paths(owned_file: str | None) -> Owned:
    """What `resolve-generated.mjs --owned` printed, or nothing when the caller
    configured no rule table. `_owned.parse` reads it, so a rule's owned
    DIRECTORY covers the files under it here exactly as it does everywhere else.
    """
    if owned_file is None:
        return EMPTY
    return parse_owned(Path(owned_file).read_text(encoding="utf-8"))


def _report_parity(ledger: ConflictSet, compare_to: str) -> None:
    """Print how this ledger differs from the caller's own conflict list.

    The ledger reads `git ls-files -u -z`; prepare.sh's arrays read
    `git diff --name-only --diff-filter=U`, which C-quotes a name holding a
    newline instead of printing its bytes. A difference between the two is
    therefore a path one of them routes and the other cannot.
    """
    listed = set(Path(compare_to).read_text(encoding="utf-8").split("\0")) - {""}
    held = {entry.path for entry in ledger.entries()}
    if held == listed:
        print(f"conflict ledger: agrees on all {len(held)} path(s)", file=sys.stderr)
        return
    print(
        "::warning::the conflict ledger and the caller's conflict list disagree: "
        f"only the ledger holds {sorted(held - listed)}, "
        f"only the caller holds {sorted(listed - held)}.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> None:
    """`--base-ref REF [--owned-file F] [--compare-to F]` prints the ledger.

    One JSON entry per conflicted path, every disposition UNCLAIMED, read from
    the index of the merge in the current directory. Nothing routes on it yet:
    prepare.sh builds it beside the whitespace-joined step outputs it is meant
    to replace, and `--compare-to` reports where the two sets differ.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--owned-file", default=None)
    parser.add_argument(
        "--compare-to",
        default=None,
        help="a file of NUL-separated paths the caller itself calls conflicted",
    )
    args = parser.parse_args(argv)

    # The merge this ledger describes is the one in the process working
    # directory, which is the only tree a step body knows it is in.
    bind_repo(Path.cwd())
    ledger = ConflictSet.from_index(
        base_remote_ref=args.base_ref, owned=_owned_paths(args.owned_file)
    )
    print(ledger.to_json())
    if args.compare_to:
        _report_parity(ledger, args.compare_to)


if __name__ == "__main__":
    main(sys.argv[1:])
