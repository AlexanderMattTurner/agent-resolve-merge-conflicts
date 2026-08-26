"""Evaluate a GitHub Actions `if:` expression against a synthetic payload.

Shared by the workflow-gate tests, which assert on what a job's real condition
DECIDES rather than on the text it is spelled with: a guard that is present but
wired into the wrong arm still reds.
"""

import json
import re


# The context roots a workflow condition may name. A step `if:` reads `steps`
# and `inputs` beside `github`, and a segment may carry a hyphen
# (`inputs.setup-command`), which no bare identifier does.
_CONTEXT_PATH = re.compile(
    r"\b(?:github|steps|inputs|needs)(?:\.[A-Za-z_][A-Za-z0-9_-]*)+"
)


def _lookup(context: dict, path: str):
    node = context
    for part in path.split("."):
        assert isinstance(node, dict) and part in node, (
            f"the workflow reads {path}, which the test payload does not model"
        )
        node = node[part]
    return node


def evaluate(expression: str, context: dict) -> bool:
    """Evaluate a GitHub `if:` expression against a payload.

    Supported: && || ! == != ( ), string literals, startsWith, contains,
    fromJSON, and `github.*` context paths. Anything else raises.
    """
    # YAML's `>-` already folds the real conditions onto one line; fold the
    # literals in this file the same way so both go through one code path.
    src = " ".join(expression.split())
    # `!=` must survive the `!` -> `not` rewrite, so park it first.
    src = src.replace("!=", "\x00")
    src = src.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
    src = src.replace("\x00", "!=")
    src = _CONTEXT_PATH.sub(lambda m: f"_ctx({json.dumps(m.group(0))})", src)
    src = src.replace("startsWith(", "_starts_with(").replace("contains(", "_contains(")
    src = src.replace("fromJSON(", "_from_json(")
    # Everything the workflow may name is now a call or a literal; a bare
    # identifier means the expression used something this evaluator does not
    # model, and guessing at it would be worse than failing.
    outside_strings = re.sub(r"'[^']*'|\"[^\"]*\"", " ", src)
    leftover = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", outside_strings)) - {
        "_ctx",
        "_starts_with",
        "_contains",
        "_from_json",
        "and",
        "or",
        "not",
        "true",
        "false",
    }
    assert not leftover, f"unsupported tokens in the expression: {sorted(leftover)}"

    env = {
        "_ctx": lambda path: _lookup(context, path),
        "_starts_with": lambda text, prefix: str(text).startswith(prefix),
        "_contains": lambda haystack, needle: needle in haystack,
        "_from_json": json.loads,
        "true": True,
        "false": False,
    }
    return bool(eval(src, {"__builtins__": {}}, env))  # noqa: S307 - fixed inputs
