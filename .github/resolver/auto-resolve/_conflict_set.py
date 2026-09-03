"""One entry per conflicted path, holding exactly one disposition.

PROBLEM CLASS — the state of a merge lives in about twenty parallel bash arrays,
so a path can sit in two partitions at once and nothing says which pass owns it.
This ledger is built ONCE from `git ls-files -u`, holds each path's three index
stages beside its classification, and records ONE disposition. A pass calls
`claim`; a second pass claiming the same path raises instead of quietly
disagreeing. `to_json` carries the whole set across a GitHub Actions step
boundary as one file, so a path whose name holds whitespace no longer has to be
dropped to keep a whitespace-joined step output readable.
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
    git,
)
from _paths import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    MergePolicy,
    PathFacts,
    classify,
)


class ClaimConflict(Exception):
    """Raised when two passes claim one path, or a pass claims one it was not
    handed off."""


class UnclaimedPaths(Exception):
    """Raised when a driver asks to finish while some path has no disposition."""


@dataclass(frozen=True, kw_only=True, slots=True)
class Stages:
    """One conflicted path's three index stages, as object ids.

    `base` is stage 1 (the merge base), `ours` stage 2, `theirs` stage 3. A
    stage git did not record is None.
    """

    base: str | None
    ours: str | None
    theirs: str | None

    def __post_init__(self) -> None:
        # INVARIANT — every stage set git can report maps to exactly one Shape.
        # A path with neither side, or an add/add missing a side, is one git
        # never writes, and admitting it would leave `Shape.of` guessing.
        if self.ours is None and self.theirs is None:
            raise ValueError("a conflicted path holds stage 2, stage 3, or both")
        if self.base is None and (self.ours is None or self.theirs is None):
            raise ValueError("a path with no stage 1 holds both added sides")


class Shape(StrEnum):
    """Which sides of the merge exist, derived from the index stages."""

    BOTH_MODIFIED = "both_modified"
    MODIFY_DELETE = "modify_delete"
    ADD_ADD = "add_add"

    @classmethod
    def of(cls, stages: Stages) -> "Shape":
        """The shape `stages` records. Total, because `Stages` refuses the
        stage sets that would have no shape."""
        if stages.base is None:
            return cls.ADD_ADD
        if stages.ours is not None and stages.theirs is not None:
            return cls.BOTH_MODIFIED
        return cls.MODIFY_DELETE


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


_STAGE_FIELD = {1: "base", 2: "ours", 3: "theirs"}


def _index_stages() -> dict[str, Stages]:
    """Every unmerged path's index stages, read NUL-terminated.

    `-z` is the whole point: git's default output QUOTES a path holding a space,
    a tab or a newline, and a reader that takes the quoted form for the name
    then acts on a file that does not exist.
    """
    found: dict[str, dict[str, str]] = {}
    for record in git("ls-files", "-u", "-z").split("\0"):
        if not record:
            continue
        meta, tab, path = record.partition("\t")
        if not tab:
            raise ValueError(f"unreadable `git ls-files -u -z` record: {record!r}")
        _mode, object_id, stage = meta.split(" ")
        field = _STAGE_FIELD[int(stage)]
        sides = found.setdefault(path, {})
        # INVARIANT — one object id per path per stage. A repeated stage would
        # make "exactly one entry per path" a question of which record won.
        if field in sides:
            raise ValueError(f"{path}: git reported stage {stage} twice")
        sides[field] = object_id
    return {
        path: Stages(
            base=sides.get("base"), ours=sides.get("ours"), theirs=sides.get("theirs")
        )
        for path, sides in found.items()
    }


class ConflictSet:
    """Every conflicted path in one merge, each with exactly one disposition."""

    def __init__(self, entries: dict[str, Entry]) -> None:
        self._entries = entries

    @classmethod
    def from_index(cls, *, base_remote_ref: str, owned: set[str]) -> "ConflictSet":
        """The ledger git's index describes right now, every path UNCLAIMED.

        `base_remote_ref` and `owned` are `_paths.classify`'s own arguments: the
        tracking ref of the branch being merged in, and the paths the calling
        repository's rule table owns.
        """
        stages = _index_stages()
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

    def claim(self, path: str, *, by: str, disposition: Disposition) -> None:
        """Record BY's decision about PATH.

        Raises `ClaimConflict` when another pass already had the last word, or
        when a DEFERRED path is claimed by a pass other than the one it names.
        """
        if disposition.by != by:
            raise ValueError(
                f"{path}: claimed by={by!r} but the disposition says "
                f"{disposition.by!r} — one pass makes one claim"
            )
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


def _owned_paths(owned_file: str | None) -> set[str]:
    if owned_file is None:
        return set()
    text = Path(owned_file).read_text(encoding="utf-8")
    return {line.strip() for line in text.splitlines() if line.strip()}


def main(argv: list[str] | None = None) -> None:
    """`--build --base-ref REF [--owned-file F]` prints the ledger as JSON.

    One entry per conflicted path, every disposition UNCLAIMED, read from the
    index of the merge in the current directory. The calling step hands that one
    file to the passes that follow, in place of nine whitespace-joined step
    outputs that cannot carry a path holding a space.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", required=True)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--owned-file", default=None)
    args = parser.parse_args(argv)

    # The merge this ledger describes is the one in the process working
    # directory, which is the only tree a step body knows it is in.
    bind_repo(Path.cwd())
    ledger = ConflictSet.from_index(
        base_remote_ref=args.base_ref, owned=_owned_paths(args.owned_file)
    )
    print(ledger.to_json())


if __name__ == "__main__":
    main(sys.argv[1:])
