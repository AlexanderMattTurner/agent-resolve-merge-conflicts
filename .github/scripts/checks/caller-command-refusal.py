#!/usr/bin/env python3
"""Every CALLER-supplied command runs through `_refusal.run_or_refuse`.

PROBLEM CLASS — the resolver runs a command the CALLING repository names, and the
runner cannot execute it. `subprocess.run(check=False)` catches a non-zero EXIT
and nothing else, and no shell stands between the call and the command, so the
interpreter RAISES before any child exists. That kills the step after the model
billed the whole resolution, reports `gave_up`, and marks the head — so every
later scan stands down until the mark's TTL expires. Twice in two days, one input
apart: agent-glovebox#4586, then `post-merge-check-command`. `run_or_refuse`
catches `OSError` and refuses with `resolver_fault=True`, leaving the head
unmarked.

The variables come from `.github/workflows/auto-resolve.yaml`, never a list here:
an input named `*-command` and the `AUTO_RESOLVE_*` keys it feeds ARE the set.

Three things fail: a read that reaches a `subprocess` call, a read that reaches
no `run_or_refuse` call at all — the second catches one handed to a helper — and
an argv built INLINE in the `subprocess` call, which carries no name to track.
Opt out with `# allow-caller-command-refusal: <reason>`.

Known gap: names match inside ONE module, so a command read in one file and run
in another passes. No current call site does that.
"""

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "resolver"))
from repolint._linecheck import run_line_checks  # noqa: E402  # pylint: disable=wrong-import-position

import yaml  # noqa: E402  # pylint: disable=wrong-import-position

_ALLOW = "allow-caller-command-refusal"
# A blank reason does NOT exempt: indistinguishable from a forgotten call site.
_ALLOW_RE = re.compile(rf"#\s*{_ALLOW}:\s*\S")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "auto-resolve.yaml"
_PACKAGE = _REPO_ROOT / ".github" / "resolver" / "auto-resolve"
_REFUSAL = "run_or_refuse"
_SUBPROCESS_CALLS = frozenset({"run", "Popen", "call", "check_call", "check_output"})


def command_env_vars(workflow: Path = _WORKFLOW) -> frozenset[str]:
    """The environment variables that carry a caller-supplied COMMAND.

    Derived, not listed: an input whose name ends `-command` is one, and every
    `env:` key whose expression names that input carries it into a step. A
    workflow that grows a third such input widens this set by itself.
    """
    spec = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    inputs = (spec.get("on") or spec.get(True) or {}).get("workflow_call", {})
    commands = [name for name in inputs.get("inputs", {}) if name.endswith("-command")]
    if not commands:
        raise RuntimeError(f"no `*-command` input found in {workflow}")
    wanted = re.compile("|".join(rf"inputs\.{re.escape(c)}\b" for c in commands))
    found: set[str] = set()
    for node in _env_blocks(spec):
        for key, value in node.items():
            if isinstance(value, str) and wanted.search(value):
                found.add(key)
    if not found:
        raise RuntimeError(f"no env var carries {commands} in {workflow}")
    return frozenset(found)


def _env_blocks(node: object) -> list[dict]:
    """Every `env:` mapping anywhere in the workflow, at any nesting depth."""
    blocks: list[dict] = []
    if isinstance(node, dict):
        env = node.get("env")
        if isinstance(env, dict):
            blocks.append(env)
        for value in node.values():
            blocks += _env_blocks(value)
    elif isinstance(node, list):
        for value in node:
            blocks += _env_blocks(value)
    return blocks


def _env_read(node: ast.AST, wanted: frozenset[str]) -> bool:
    """Whether NODE reads one of the WANTED environment variables, at any depth —
    `os.environ.get(X, "")`, `os.environ[X]`, and either wrapped in `shlex.split`."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and sub.value in wanted:
            return True
    return False


def _argv_names(call: ast.Call) -> set[str]:
    """The plain names appearing in CALL's first positional argument, so
    `[*PRE_PASS, *args]` and a bare `argv` both answer with their own name."""
    if not call.args:
        return set()
    return {n.id for n in ast.walk(call.args[0]) if isinstance(n, ast.Name)}


def _assignment(node: ast.AST) -> tuple[list[ast.expr], ast.expr | None]:
    """NODE's assignment targets and value, for a plain or an ANNOTATED binding.

    An annotated one is the same read wearing a type: this package already writes
    annotated module constants (`_lockfiles.py`, `_git_io.py`), so reading only
    `ast.Assign` would let one refactor hide a call site.
    """
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    return [], None


def _called_name(call: ast.Call) -> tuple[str, str]:
    """CALL's (module, attribute) for `mod.attr(...)`, or ("", name) for `name(...)`."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return getattr(func.value, "id", ""), func.attr
    return "", getattr(func, "id", "")


def violations(text: str, wanted: frozenset[str] | None = None) -> list[int]:
    """1-based lines that read a caller-supplied command unsafely."""
    wanted = command_env_vars() if wanted is None else wanted
    tree = ast.parse(text)
    lines = text.splitlines()
    reads: dict[str, int] = {}
    for node in ast.walk(tree):
        targets, value = _assignment(node)
        if value is None or not _env_read(value, wanted):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                reads.setdefault(target.id, node.lineno)
    refused: set[str] = set()
    unguarded: dict[str, int] = {}
    inline: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        module, attr = _called_name(node)
        runs_it = module == "subprocess" and attr in _SUBPROCESS_CALLS
        # An argv built in the call itself binds no name, so the loop above never
        # saw it — and it is the plainest form of the defect this check exists for.
        if runs_it and node.args and _env_read(node.args[0], wanted):
            inline.add(node.lineno)
        names = _argv_names(node) & reads.keys()
        if not names:
            continue
        if attr == _REFUSAL:
            refused |= names
        elif runs_it:
            for name in names:
                unguarded.setdefault(name, node.lineno)
    # The subprocess line wins when a name is both unguarded and never refused:
    # it is the call to rewrite, where the read is only where the value came from.
    hits = {**{n: ln for n, ln in reads.items() if n not in refused}, **unguarded}
    return sorted(
        lineno
        for lineno in set(hits.values()) | inline
        if not any(
            _ALLOW_RE.search(lines[n - 1])
            for n in (lineno, lineno - 1)
            if 1 <= n <= len(lines)
        )
    )


def main(argv: list[str]) -> None:
    wanted = command_env_vars()
    paths = [str(p) for p in sorted(_PACKAGE.rglob("*.py"))] if not argv else argv
    sys.exit(
        run_line_checks(
            paths,
            lambda text: violations(text, wanted),
            "this reads a caller-supplied command but does not run it through "
            f"`_refusal.{_REFUSAL}` — a binary the runner cannot execute then "
            "RAISES past `check=False`, losing a resolution the model was "
            "already billed for.",
            remedy=(
                f"route the command through `{_REFUSAL}`, or annotate "
                f"`# {_ALLOW}: <reason>`."
            ),
        )
    )


if __name__ == "__main__":
    main(sys.argv[1:])
