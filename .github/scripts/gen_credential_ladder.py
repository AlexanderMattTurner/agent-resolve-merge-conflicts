#!/usr/bin/env python3
"""Render every unrolled copy of the credential ladder from `lib_credential_ladder.rungs()`.

GitHub Actions cannot loop `uses:` steps and cannot index `secrets.*` by a computed
name, so the ladder is unrolled text by necessity. That makes it a BUILD-TIME
artifact rather than a hand-maintained one: this script owns the marked regions,
the `gen-credential-ladder` pre-commit hook keeps them current, and
`--check` reports drift without writing, which is how CI asserts the committed
regions round-trip.

The order lives in three regions of .github/workflows/auto-resolve.yaml, so a rung added to
`lib/shared-names.json` reaches every one of them at once.

Run with no argument to write, or `--check` to report drift and write nothing.
"""

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# The resolver tree holds the ONE ladder model. This generator renders the
# workflow's unrolled copy from it, and `auto-resolve/run-ladder.py` walks the
# same file at run time — a second copy here would let the rungs the workflow
# declares drift from the rungs the walker spends.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resolver"))
# pylint: disable=wrong-import-position  # must follow the sys.path insert above
from lib_credential_ladder import (  # noqa: E402  (path inserted just above)
    RungSpec,
    rungs,
)

from repolint._root import repo_root  # noqa: E402  (path inserted just above)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from lib_marked_region import (  # noqa: E402  (path inserted just above)
    region_begin,
    region_end,
    splice,
)

REPO_ROOT = repo_root(Path(__file__))
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _where(kind: str) -> str:
    return f"credential ladder {kind}"


def begin_marker(kind: str) -> str:
    """The opening marker line for a region of KIND, without its indentation."""
    return region_begin(
        _where(kind),
        ".github/scripts/gen_credential_ladder.py",
        note="do not edit by hand",
    )


def end_marker(kind: str) -> str:
    """The closing marker line for a region of KIND, without its indentation."""
    return region_end(_where(kind))


def splice_all(doc: str, *, begin: str, end: str, block: str, label: str) -> str:
    """`doc` with EVERY region between the two markers replaced by `block`.

    lib_marked_region.splice resolves the FIRST pair only, which is right for a
    document holding one region and wrong for the four identical credential blocks
    in one document. This walks the document and delegates each replacement,
    so the refusal on a missing or reversed marker pair stays where it is defined.
    """
    done, rest = "", doc
    while begin in rest:
        spliced = splice(rest, begin=begin, end=end, block=block, label=label)
        cut = spliced.index(end, spliced.index(begin)) + len(end)
        done, rest = done + spliced[:cut], spliced[cut:]
    if not done:
        raise ValueError(f"{label}: begin marker not found: {begin}")
    return done + rest


def region_indent(doc: str, begin: str, label: str) -> str:
    """The whitespace every occurrence of the region's begin marker sits at.

    INVARIANT — every occurrence must agree. `splice_all` renders one block for
    every marker pair in the document, so two markers at different indents would
    leave one of them silently misindented in the committed YAML.
    """
    indents = []
    start = doc.find(begin)
    if start == -1:
        raise ValueError(f"{label}: begin marker not found: {begin}")
    while start != -1:
        line_start = doc.rfind("\n", 0, start) + 1
        indents.append(doc[line_start:start])
        start = doc.find(begin, start + len(begin))
    if len(set(indents)) > 1:
        raise ValueError(
            f"{label}: begin marker sits at {len(set(indents))} different indents "
            f"across {len(indents)} occurrences; splice_all renders one block for all"
        )
    return indents[0]


@dataclass(frozen=True)
class Ctx:
    """What a region's renderer is handed: the ladder, the indent its block sits at,
    and the document it replaces a region in — which is where the action pin lives."""

    ladder: tuple[RungSpec, ...]
    indent: str
    doc: str
    label: str


@dataclass(frozen=True)
class Region:
    """One marked region: which file it sits in, what kind it is, and how it renders."""

    path: Path
    kind: str
    render: Callable[["Ctx"], str]


def _mapping_block(
    ladder: tuple[RungSpec, ...],
    indent: str,
    key_fn: Callable[[RungSpec], str],
    value_fn: Callable[[RungSpec], str],
) -> str:
    """One `key: ${{ value }}` line per rung — the shape every simple ladder mapping shares."""
    return "\n".join(
        f"{indent}{key_fn(rung)}: ${{{{ {value_fn(rung)} }}}}" for rung in ladder
    )


def secrets_map_block(ctx: Ctx) -> str:
    """One `NAME: ${{ secrets.NAME }}` row per rung, for the pre-bundle self-review step.

    That step needs every ladder credential rather than only the primary — giving it
    just the primary is what took the resolver down for every PR once that one token
    expired.
    """
    return _mapping_block(
        ctx.ladder, ctx.indent, lambda r: r.env_var, lambda r: f"secrets.{r.env_var}"
    )


@dataclass(frozen=True)
class Step:
    """A rendered step's identity: its display name, its `id:`, and what gates it.

    NOTE is a comment block that sits directly above the `if:` line, so it travels
    with the gate rather than with the step's body.
    """

    name: str
    step_id: str
    gate: str = ""
    note: str = ""


# --- auto-resolve-conflicts.yaml ------------------------------------------------
#
# The resolve job keeps its own rungs rather than calling claude-code-with-fallback:
# it checks out the UNTRUSTED PR head mid-merge, and the runner reads a local
# composite's manifest out of that workspace at step time — so a conflicted manifest
# would kill every rung before the resolver starts. It shares this source, not that
# renderer. `auto-resolve/run-ladder.py` walks the rungs as a LOOP, so the only thing
# left unrolled here is what an expression cannot compute: a `secrets.*` name.


def rung_tokens_block(ctx: Ctx) -> str:
    """Every rung's credential VALUE, in the loop step's env as `RUNG_<i>_TOKEN`.

    `secrets.*` takes no computed name, so the loop cannot fetch a rung's token itself.
    This mapping is the whole of what auto-resolve's ladder still unrolls, and
    run-ladder.py hands each attempt's CHILD exactly one entry from it.
    """
    return _mapping_block(
        ctx.ladder,
        ctx.indent,
        lambda r: f"RUNG_{r.index}_TOKEN",
        lambda r: f"secrets.{r.env_var}",
    )


PREFERRED_NOTE = """\
{i}# The resolve ladder already proved one credential can reach the model. Review
{i}# with it first instead of replaying each exhausted rung. The loop publishes the
{i}# WINNING rung's own secret NAME — a winner whose own secret is unset (the rung-2
{i}# same-credential free retry) names the rung before it, per
{i}# auto-resolve/_ladder.py's `preferred_token_env`. These arms turn that name into
{i}# its value, which no expression can index by name.
"""


def preferred_token_block(ctx: Ctx) -> str:
    """The winning rung's own credential, selected by the NAME the loop published.

    INVARIANT — every arm pairs a name with the secret of that SAME name. A mispaired
    arm would hand the pre-bundle self-review a credential the ladder never proved,
    which reads as a review that failed on its own merits rather than on a dead token.
    """
    ladder, indent = ctx.ladder, ctx.indent
    published = "steps.ladder.outputs.preferred_token_env"
    arms = [
        f"{indent}  ${{{{ {published} == '{ladder[0].env_var}'"
        f" && secrets.{ladder[0].env_var}"
    ]
    arms += [
        f"{indent}  || {published} == '{rung.env_var}' && secrets.{rung.env_var}"
        for rung in ladder[1:]
    ]
    arms.append(f"{indent}  || '' }}}}")
    return (
        PREFERRED_NOTE.format(i=indent)
        + f"{indent}RESOLVER_PREFERRED_TOKEN: >-\n"
        + "\n".join(arms)
    )


AUTO_RESOLVE = WORKFLOWS / "auto-resolve.yaml"

# Three regions in one file. This repository ships the reusable resolver and
# nothing else that unrolls the ladder, so the composite actions and the release
# workflows glovebox also generates into have no counterpart here.
REGIONS = (
    Region(AUTO_RESOLVE, "rung-tokens", rung_tokens_block),
    Region(AUTO_RESOLVE, "bundle-secrets", secrets_map_block),
    Region(AUTO_RESOLVE, "preferred-token", preferred_token_block),
)


def render_doc(path: Path, doc: str, ladder: tuple[RungSpec, ...]) -> str:
    """DOC with every region PATH owns re-rendered. Pure: reads no file and writes none."""
    for region in REGIONS:
        if region.path != path:
            continue
        begin, end = begin_marker(region.kind), end_marker(region.kind)
        label = f"{path}: {region.kind}"
        indent = region_indent(doc, begin, label)
        doc = splice_all(
            doc,
            begin=begin,
            end=end,
            block=region.render(
                Ctx(ladder=ladder, indent=indent, doc=doc, label=label)
            ),
            label=label,
        )
    return doc


def touched_paths() -> tuple[Path, ...]:
    """Every distinct file a region targets, in first-appearance order."""
    return tuple(dict.fromkeys(region.path for region in REGIONS))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift on stderr and write nothing; exit non-zero when a region is stale",
    )
    args = parser.parse_args()

    ladder = rungs()
    stale = []
    for path in touched_paths():
        original = path.read_text(encoding="utf-8")
        text = render_doc(path, original, ladder)
        if text == original:
            continue
        stale.append(path)
        if not args.check:
            path.write_text(text, encoding="utf-8")
    if args.check and stale:
        names = "\n  ".join(str(p.relative_to(REPO_ROOT)) for p in stale)
        raise SystemExit(
            "the credential ladder's generated regions are stale in:\n  "
            f"{names}\nRun: uv run python .github/scripts/gen_credential_ladder.py"
        )


if __name__ == "__main__":
    main()
