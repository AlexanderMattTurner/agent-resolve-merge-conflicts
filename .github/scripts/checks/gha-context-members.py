#!/usr/bin/env python3
"""Ban a `${{ }}` read of a context member GitHub does not populate.

GitHub substitutes the EMPTY STRING for a context property it does not define,
so a misspelled or unpopulated member is never an error — it is a silent "".
The resolver read `github.job_workflow_sha`, which GitHub documents but has
never populated (actions/runner#2417), and for a day every run cloned an empty
ref. actionlint cannot answer this: it types each context from its own member
table, so it reports a member the table lacks and accepts one the table has,
and neither says whether the RUNNER fills the member in.

A VIOLATION is a dotted read rooted at a CLOSED context whose member is absent
from the table below, or listed there as one the runner never fills:

- `github`, `job`, `runner`, `strategy` — the member set is the whole context.
- `needs.<id>` and `steps.<id>` — checked one level in, where the set closes.
- everything under `github.event` is the webhook payload, so it is free, as are
  the caller-defined `env`, `inputs`, `vars`, `secrets` and `matrix`.

Both expression forms are read: a `${{ … }}` inside any YAML scalar, and a bare
`if:` condition, which GitHub evaluates as an expression without the braces. The
file is parsed as YAML rather than scanned as text, so `${{ }}` written in a
COMMENT — this tree explains the interpolation risk that way — is not a read.

Exempt with `# allow-gha-context: <reason>` on the line or the line above.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _gha_expression import (  # noqa: E402  # pylint: disable=wrong-import-position
    context_reads,
    parse,
    parse_condition,
)

_ANNOTATION_RE = re.compile(r"#\s*allow-gha-context:\s*\S")
_INTERPOLATION_RE = re.compile(r"\$\{\{(?P<body>.*?)\}\}", re.DOTALL)

# Every member the runner fills in, per context. A member GitHub documents but
# leaves empty belongs in _NEVER_POPULATED instead, with the remedy.
_MEMBERS = {
    "github": frozenset(
        {
            "action",
            "action_path",
            "action_ref",
            "action_repository",
            "action_status",
            "actor",
            "actor_id",
            "api_url",
            "base_ref",
            "env",
            "event",
            "event_name",
            "event_path",
            "graphql_url",
            "head_ref",
            "job",
            "path",
            "ref",
            "ref_name",
            "ref_protected",
            "ref_type",
            "repository",
            "repository_id",
            "repository_owner",
            "repository_owner_id",
            "repositoryUrl",
            "retention_days",
            "run_attempt",
            "run_id",
            "run_number",
            "secret_source",
            "server_url",
            "sha",
            "token",
            "triggering_actor",
            "workflow",
            "workflow_ref",
            "workflow_sha",
            "workspace",
        }
    ),
    "job": frozenset({"container", "services", "status", "workflow_sha"}),
    "runner": frozenset(
        {"arch", "debug", "environment", "name", "os", "temp", "tool_cache"}
    ),
    "strategy": frozenset({"fail-fast", "job-index", "job-total", "max-parallel"}),
}

# Contexts keyed by a job or step id, whose member set closes one level in.
_KEYED_MEMBERS = {
    "needs": frozenset({"outputs", "result"}),
    "steps": frozenset({"conclusion", "outcome", "outputs"}),
}

# Documented, and empty at run time. The value is the remedy the message prints.
_NEVER_POPULATED = {
    "github.job_workflow_sha": (
        "the `github` context inside a reusable workflow is the CALLER's, so it "
        "carries no member for the called workflow's own commit "
        "(actions/runner#2417) — read `job.workflow_sha`"
    ),
}


def _violation(path: str) -> str | None:
    """The message for PATH, or None when the read is one GitHub answers."""
    if path in _NEVER_POPULATED:
        return f"`{path}` reads empty at run time: {_NEVER_POPULATED[path]}"
    parts = path.split(".")
    root, members = parts[0], parts[1:]
    if not members:
        return None
    if root in _MEMBERS:
        # The webhook payload is open-ended, so only the first hop is closed.
        if root == "github" and members[0] == "event":
            return None
        if members[0] in _MEMBERS[root]:
            return None
        return (
            f"`{root}.{members[0]}` is not a member of the `{root}` context, so "
            f"GitHub evaluates `{path}` to the empty string"
        )
    if root in _KEYED_MEMBERS and len(members) >= 2:
        if members[1] in _KEYED_MEMBERS[root]:
            return None
        return (
            f"`{members[1]}` is not a member of `{root}.<id>`, so GitHub "
            f"evaluates `{path}` to the empty string"
        )
    return None


def _annotated(lines: list[str], lineno: int) -> bool:
    """True when the exemption sits on LINENO (1-based) or the line above it."""
    window = lines[max(0, lineno - 2) : lineno]
    return any(_ANNOTATION_RE.search(line) for line in window)


def _expressions(text: str):
    """Each expression TEXT contains, as (parse tree, 1-based line).

    Two forms reach a context: an interpolation inside a scalar, and a bare
    `if:` value, which GitHub evaluates as an expression with no braces around
    it. Both come from the YAML node tree, so a comment is never a read.
    """
    for document in yaml.compose_all(text):
        for node in _walk(document):
            if isinstance(node, yaml.ScalarNode):
                # The RAW slice, not `node.value`: a block scalar's marks span
                # its indicator and indentation, so only an offset measured in
                # the source lands the report on the line the reader edits.
                start = node.start_mark.index
                raw = text[start : node.end_mark.index]
                for match in _INTERPOLATION_RE.finditer(raw):
                    line = text.count("\n", 0, start + match.start()) + 1
                    yield parse(match.group("body")), line
            if not isinstance(node, yaml.MappingNode):
                continue
            for key, value in node.value:
                if (
                    isinstance(key, yaml.ScalarNode)
                    and key.value == "if"
                    and isinstance(value, yaml.ScalarNode)
                    and "${{" not in value.value
                ):
                    yield parse_condition(value.value), value.start_mark.line + 1


def _walk(node):
    yield node
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            yield from _walk(key)
            yield from _walk(value)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            yield from _walk(item)


def violations(text: str) -> list[tuple[int, str]]:
    """Every (1-based line, message) pair in TEXT."""
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    for tree, lineno in _expressions(text):
        for path in sorted(context_reads(tree)):
            message = _violation(path)
            if message and not _annotated(lines, lineno):
                found.append((lineno, message))
    return found


def main(argv: list[str]) -> int:
    status = 0
    for path in argv:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, message in violations(text):
            print(f"{path}:{lineno}: {message}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
