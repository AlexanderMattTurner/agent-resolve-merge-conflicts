"""PROBLEM CLASS — two conflict blocks of one definition where one is the CODE and
the other is the PROSE arguing for it, cut apart and given to different shards.

A docstring paragraph that says why a technique is right is not separable from the
technique: each side's paragraph argues for its own side's body. Two shards then
decide the pair independently, and the honest answer for the prose shard is to
decline — PR #4871's `tests/test_safe_launch_latency.py` declined for exactly that
reason, on a file whose code block was decidable.

So the prose does not get a shard. The CODE block decides, and the prose is taken
from whichever side won it, mechanically and with no model call. A code block that
declines takes its prose with it: the pair keeps its markers, and the file carries
the unresolved conflict to the marker sweep as before.

Scope is Python, because the pairing needs a parser that answers two questions about
a file mid-merge: which lines are prose, and which definition a line sits in.
`tokenize` and `ast` answer both. A format with no such reader here is left alone,
which is today's behaviour.
"""

import ast
import io
import token
import tokenize
from dataclasses import dataclass
from pathlib import Path

from _conflict_hunks import OURS, THEIRS, Hunk, hunks_of, segments, side_of

# Token types that carry no code. Everything else on a line makes that line CODE,
# which is what tells a docstring apart from an ordinary string in an expression.
_QUIET = frozenset(
    {
        token.NL,
        token.NEWLINE,
        token.INDENT,
        token.DEDENT,
        token.ENDMARKER,
        token.ENCODING,
    }
)

PROSE, CODE = "prose", "code"


@dataclass(frozen=True)
class Side:
    """One whole-side view of a conflicted file: the text with every block replaced
    by that side, and where each block's own lines landed in it."""

    text: str
    lines: dict[int, range]


def _side_view(text: str, which: int) -> Side | None:
    """TEXT with every block replaced by side WHICH, and each block's line range.

    Built from the same `segments` walk `splice` uses, so the ranges are the lines
    the block really occupies rather than an offset guess.
    """
    parts = segments(text)
    if parts is None:
        return None
    out: list[str] = []
    lines: dict[int, range] = {}
    at = 1
    for part in parts:
        chunk = part if isinstance(part, str) else side_of(part.text, which)
        count = len(chunk.splitlines())
        if isinstance(part, Hunk):
            lines[part.ordinal] = range(at, at + count)
        out.append(chunk)
        at += count
    return Side(text="".join(out), lines=lines)


def _line_kinds(text: str) -> dict[int, str] | None:
    """Every line of TEXT as PROSE or CODE, or None when it does not tokenize.

    A line is PROSE when a comment or a string covers it and nothing else does, so a
    docstring paragraph is prose and `name = "a string"` is code.
    """
    kinds: dict[int, str] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in _QUIET:
                continue
            quiet = tok.type in (token.COMMENT, token.STRING)
            for line in range(tok.start[0], tok.end[0] + 1):
                if not quiet or kinds.get(line) == CODE:
                    kinds[line] = CODE
                elif line not in kinds:
                    kinds[line] = PROSE
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return None
    return kinds


def _enclosing(text: str) -> dict[int, str] | None:
    """Every line's innermost definition, by qualified name, or None when TEXT does
    not parse. Named rather than numbered so the two side views compare: the same
    definition sits at different line numbers on each side."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    owner: dict[int, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ) and getattr(child, "end_lineno", None):
                name = f"{prefix}.{child.name}" if prefix else child.name
                for line in range(child.lineno, child.end_lineno + 1):
                    owner[line] = name
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, "")
    return owner


def _classify(view: Side, blocks: list[Hunk]) -> dict[int, tuple[str, str]] | None:
    """Each block's kind and enclosing definition in VIEW, or None with no verdict.

    A block whose side is EMPTY gets no kind: there are no lines to read, and one
    side deleting the paragraph is not the pair this module acts on.
    """
    kinds = _line_kinds(view.text)
    owners = _enclosing(view.text)
    if kinds is None or owners is None:
        return None
    out: dict[int, tuple[str, str]] = {}
    for block in blocks:
        span = view.lines.get(block.ordinal, range(0))
        seen = {kinds.get(line) for line in span if kinds.get(line)}
        if not seen:
            continue
        owned = {owners.get(line, "") for line in span}
        out[block.ordinal] = (
            CODE if CODE in seen else PROSE,
            owned.pop() if len(owned) == 1 else "",
        )
    return out


def follower_pairs(path: str, text: str) -> dict[int, int]:
    """{prose block: the code block that decides it} for the conflicted file TEXT.

    A pair is made only when BOTH sides agree: both read the one block as prose,
    both read the other as code, and both put the two in the same definition. A
    disagreement means the two sides are not arguing about one thing, and the blocks
    stay independent — which is what every file outside this shape gets.
    """
    if Path(path).suffix != ".py":
        return {}
    blocks = hunks_of(text)
    if len(blocks) < 2:
        return {}
    views = [_side_view(text, side) for side in (OURS, THEIRS)]
    if any(view is None for view in views):
        return {}
    reads = [_classify(view, blocks) for view in views if view is not None]
    if any(read is None for read in reads):
        return {}
    agreed = {
        ordinal: reads[0][ordinal]
        for ordinal in reads[0]  # type: ignore[union-attr]
        if reads[1] is not None and reads[1].get(ordinal) == reads[0][ordinal]  # type: ignore[index]
    }
    pairs: dict[int, int] = {}
    for ordinal, (kind, owner) in agreed.items():
        if kind != PROSE or not owner:
            continue
        partners = [
            other
            for other, (other_kind, other_owner) in agreed.items()
            if other_kind == CODE and other_owner == owner
        ]
        if partners:
            # The nearest, and the one BEFORE it on a tie: a docstring argues for the
            # body under it far more often than for the definition above it.
            pairs[ordinal] = min(
                partners, key=lambda other: (abs(other - ordinal), other)
            )
    return pairs


def winning_side(block: str, resolved: str) -> int | None:
    """Which side RESOLVED took of BLOCK, or None when it took neither.

    None is the answer a blend produces, and it is what stops the prose following a
    resolution that is not either side's: the prose block keeps its markers instead,
    and the residue pass reads the file with the code block already settled.
    """
    for side in (OURS, THEIRS):
        if resolved.strip() == side_of(block, side).strip():
            return side
    return None


def pairs_for_file(file: str) -> dict[int, int]:
    """FILE's {prose block: the code block that decides it}. Empty for a path this
    run cannot read as text — the same refusal the block reader makes."""
    try:
        return follower_pairs(file, Path(file).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return {}


def follow_code(
    file: str, pairs: dict[int, int], resolved: dict[int, str]
) -> tuple[dict[int, str], list[str]]:
    """What FILE's prose blocks take given what its code blocks resolved to, and one
    log line per pair.

    The prose follows the side that won its code block, so the paragraph and the
    implementation it argues for come from one branch. Three answers leave the prose
    its markers: a code block that declined, one nobody delivered, and one whose
    resolution blends both sides — none names a side to follow.
    """
    if not pairs:
        return {}, []
    blocks = {
        block.ordinal: block
        for block in hunks_of(Path(file).read_text(encoding="utf-8"))
    }
    followed: dict[int, str] = {}
    notices: list[str] = []
    for prose, code in sorted(pairs.items()):
        answer = resolved.get(code)
        side = None if answer is None else winning_side(blocks[code].text, answer)
        if side is None:
            notices.append(
                f"{file} block {prose} is the prose arguing for block {code}, "
                "which named no side; it keeps its conflict markers."
            )
            continue
        followed[prose] = side_of(blocks[prose].text, side)
        notices.append(
            f"{file} block {prose} is prose arguing for block {code}, so it "
            "follows the side that block resolved to."
        )
    return followed, notices
