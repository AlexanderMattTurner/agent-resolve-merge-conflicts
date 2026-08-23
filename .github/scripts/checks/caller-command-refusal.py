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

A read and its run may sit in different modules, so the name graph is built over
the WHOLE package: every name any module binds from a `*-command` read is a
carrier, and a module that imports one is held to the same three rules.
"""

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _linecheck import run_line_checks  # noqa: E402  # pylint: disable=wrong-import-position

import yaml  # noqa: E402  # pylint: disable=wrong-import-position

_ALLOW = "allow-caller-command-refusal"
# A blank reason does NOT exempt: indistinguishable from a forgotten call site.
_ALLOW_RE = re.compile(rf"#\s*{_ALLOW}:\s*\S")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "auto-resolve.yaml"
_PACKAGE = _REPO_ROOT / ".github" / "resolver" / "auto-resolve"
_REFUSAL = "run_or_refuse"
_SUBPROCESS_CALLS = frozenset({"run", "Popen", "call", "check_call", "check_output"})
# A binding inside one of these belongs to that scope, not to the one around it.
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


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


def _parameter_aliases(node: ast.AST, first: str) -> frozenset[str]:
    """FIRST plus every name the function rebinds from it, to a fixed point.

    A raw branch is free to rename the parameter before running it, and matching
    the parameter's own spelling alone would read that branch as touching no
    command at all — leaving the helper classified as a refusal.
    """
    held = {first}
    while True:
        grown = set(held)
        for child in _own_scope(node):
            targets, value = _assignment(child)
            if value is not None and _renames_a_carrier(value, grown):
                grown |= {t.id for t in targets if isinstance(t, ast.Name)}
        if grown == held:
            return frozenset(held)
        held = grown


def _refusing_helpers(tree: ast.AST) -> frozenset[str]:
    """Functions whose FIRST parameter reaches `run_or_refuse` and NO raw runner.

    `_read_the_tree(argv)` routes its own parameter through the refusal, so a
    carrier handed to it IS refused — and only reading call sites named
    `run_or_refuse` misses that.

    A helper that also executes that parameter through `subprocess` is not
    refusing, whichever branch holds which call: a caller handing it a command
    is refused on one path and runs raw on the other, so counting the helper as
    a refusal clears exactly the call site this check exists for.

    Own scope only, in two directions. A `run_or_refuse` inside a NESTED function
    is that function's, and it may never run. And only a MODULE-LEVEL definition
    is collected: a nested `execute` that refuses would otherwise put `execute`
    in a name set the module-level `execute` shares, clearing every call to the
    one that runs the command raw.

    The parameter is followed through the helper's own renames, so a raw branch
    that does `command = argv` first is still a raw branch. And a branch that
    hands the parameter to ANOTHER local helper only refuses if that helper does,
    which is why the answer is a fixed point: `execute` calling `unsafe(argv)`
    refuses nothing, however many `run_or_refuse` calls its other branch holds.
    """
    handoffs = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional = [*node.args.posonlyargs, *node.args.args]
        if positional:
            handoffs[node.name] = _parameter_handoffs(node, positional[0].arg)
    # Greatest fixed point: assume every candidate refuses, then drop the ones a
    # surviving target does not cover. Assuming the reverse would let a pair of
    # mutually-recursive helpers refuse nothing while both pass.
    # A non-empty target list: a helper the parameter reaches no call from
    # refuses nothing, and an `all()` over its empty list would say otherwise.
    refusing = {name for name, targets in handoffs.items() if targets}
    while True:
        dropped = {
            name
            for name in refusing
            if not all(
                target == _REFUSAL or target in refusing
                for target in handoffs[name] or ()
            )
        }
        if not dropped:
            return frozenset(refusing)
        refusing -= dropped


def _parameter_handoffs(node: ast.AST, first: str) -> list[str] | None:
    """Every call this function passes its FIRST parameter to, as argv, by name —
    or ``None`` when a `subprocess` runner is one of them, which no target set
    can rescue. An empty list means the parameter reaches no call at all, so the
    function refuses nothing.
    """
    held = _parameter_aliases(node, first)
    targets: list[str] = []
    for call in _own_scope(node):
        if not isinstance(call, ast.Call):
            continue
        module, attr = _called_name(call)
        argv = _argv_of(call)
        if argv is None or not any(
            isinstance(sub, ast.Name) and sub.id in held for sub in ast.walk(argv)
        ):
            continue
        if module == "subprocess" and attr in _SUBPROCESS_CALLS:
            return None
        targets.append(attr)
    return targets


def _argv_of(call: ast.Call, parameter: str | None = None) -> ast.expr | None:
    """CALL's argv: its first positional argument, or the keyword that names it.

    `subprocess.run` accepts `args=` as well as the positional form, so reading
    only the positional one lets the keyword spelling run a caller's command
    with nothing flagged. A local helper takes its own PARAMETER name instead —
    `execute(argv=PRE_PASS)` hands the command over exactly as the positional
    call does, and the helper is free to call that parameter anything.
    """
    wanted = {"args"} if parameter is None else {"args", parameter}
    return next(
        (kw.value for kw in call.keywords if kw.arg in wanted),
        call.args[0] if call.args else None,
    )


def _first_parameters(tree: ast.AST) -> dict[str, str]:
    """{function name: its first positional parameter}, for the keyword form of
    a handoff. Module-local, like {@link _refusing_helpers}: a helper defined
    elsewhere is read when that module is checked."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional = [*node.args.posonlyargs, *node.args.args]
        if positional:
            out.setdefault(node.name, positional[0].arg)
    return out


def _own_scope(node: ast.AST) -> list[ast.AST]:
    """NODE's descendants, stopping at every nested scope boundary.

    `ast.walk` crosses into a nested function, so an assignment there would read
    as a rebinding out here — and a name the enclosing scope still resolves to
    the module-level carrier would drop out of the carrier set.
    """
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        out.append(child)
        if isinstance(child, _SCOPES):
            continue
        stack.extend(ast.iter_child_nodes(child))
    return out


def _shadowed_in(
    node: ast.AST, carriers: set[str], wanted: frozenset[str]
) -> frozenset[str]:
    """Carrier names a nearer binding takes over inside NODE's own body: its
    parameters, and anything it assigns from a value that is neither a carrier
    rename nor a caller-command read.

    The read is the carve-out that matters: a function that binds `argv` from
    the environment IS the carrier's origin, so counting that as shadowing hides
    the very call site this check exists for.

    Nested functions, lambdas and classes are their own scopes, so a binding
    there takes nothing over out here. `visit` reaches them separately with
    their own shadow set.
    """
    taken: set[str] = set()
    arguments = getattr(node, "args", None)
    if isinstance(arguments, ast.arguments):
        taken |= {
            arg.arg
            for group in (
                arguments.posonlyargs,
                arguments.args,
                arguments.kwonlyargs,
                [arguments.vararg] if arguments.vararg else [],
                [arguments.kwarg] if arguments.kwarg else [],
            )
            for arg in group
        }
    for child in _own_scope(node):
        targets, value = _assignment(child)
        if (
            value is None
            or _renames_a_carrier(value, carriers)
            or _env_read(value, wanted)
        ):
            continue
        taken |= {t.id for t in targets if isinstance(t, ast.Name)}
    return frozenset(taken & carriers)


def _argv_names(
    call: ast.Call,
    modules: dict[str, str] | None = None,
    parameter: str | None = None,
) -> set[str]:
    """The plain names appearing in CALL's argv, so `[*PRE_PASS, *args]` and a
    bare `argv` both answer with their own name.

    The argv is the first positional argument OR the `args=` keyword, which
    `subprocess.run` accepts equally — reading only the positional one lets the
    keyword spelling run a caller's command with nothing flagged.

    A `bundle.PRE_PASS` reached through a MODULES alias answers with `PRE_PASS`
    too: an imported carrier is the same value under another spelling.
    """
    argv = _argv_of(call, parameter)
    if argv is None:
        return set()
    found = set()
    for node in ast.walk(argv):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and getattr(node.value, "id", "") in (
            modules or {}
        ):
            found.add(node.attr)
    return found


def _imports(
    tree: ast.AST, package_modules: frozenset[str] = frozenset()
) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """({bound name: (defining module, SOURCE name)}, {module alias: module}).

    Provenance matters twice. A symbol name alone makes
    `from thirdparty import COMMAND` this package's carrier the moment any
    module here exports that name, and it makes two package modules that happen
    to share a constant name into one carrier. Both refuse a value the caller
    never supplied, so the defining module travels with every binding and only
    a relative import — or one naming a module this package defines — carries a
    carrier out at all.

    The bound name is kept as the key because that is the spelling the argv scan
    reports: `import X as Y` binds the carrier under Y, and matching the bound
    name against a set of source names intersects to nothing, failing OPEN.
    """
    direct: dict[str, tuple[str, str]] = {}
    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            parts = (node.module or "").split(".")
            if not node.level and parts[0] not in package_modules:
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                direct[bound] = (parts[-1], alias.name)
                # `from . import bundle` binds a MODULE, so `bundle.PRE_PASS`
                # reaches a carrier through an attribute the name map misses.
                if node.level and node.module is None:
                    modules[bound] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in package_modules:
                    modules[(alias.asname or alias.name).split(".")[0]] = root
    return direct, modules


def package_modules(package: Path = _PACKAGE) -> frozenset[str]:
    """The module names this package defines, for the provenance check above."""
    return frozenset(path.stem for path in package.rglob("*.py"))


def command_names(
    wanted: frozenset[str], package: Path = _PACKAGE
) -> frozenset[tuple[str, str]]:
    """Every (module, name) any module in PACKAGE binds from a caller-command read.

    The check runs per file, so without this a carrier read in one module and
    run in another sits in nobody's view: the reading module refuses it (or
    hands it to a helper) and looks clean, and the running module never saw an
    env read to bind the name to. A module that IMPORTS one of these names FROM
    the module that binds it is held to the same rules as that module.

    The module travels with the name because a name alone would merge two
    unrelated constants that happen to share a spelling, and refuse a value no
    caller supplied.
    """
    modules_by_stem = {
        path.stem: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(package.rglob("*.py"))
    }
    defined = package_modules(package)
    found = {
        (stem, name)
        for stem, tree in modules_by_stem.items()
        for name in _module_level_reads(tree, wanted)
    }
    # Re-exports, to a fixed point. `reader.py` binds PRE_PASS, `middle.py` does
    # `from .reader import PRE_PASS as COMMAND`, and `runner.py` imports COMMAND
    # — whose SOURCE name is COMMAND, not PRE_PASS. One hop of alias resolution
    # leaves that second hop invisible, so the carrier set closes over every hop
    # a module could add.
    while True:
        grown = set(found)
        for stem, tree in modules_by_stem.items():
            direct, _modules = _imports(tree, defined)
            grown |= {
                (stem, bound) for bound, origin in direct.items() if origin in grown
            }
            here = {name for module, name in grown if module == stem}
            # A carrier this module reaches as `bundle.PRE_PASS` is one it can
            # re-export, so the qualified name counts beside its own bindings.
            through = {name for module, name in grown if module in _modules.values()}
            grown |= {
                (stem, name)
                for name in _module_level_rebinds(tree, here | through, _modules)
            }
        if grown == found:
            return frozenset(found)
        found = grown


def _module_level_rebinds(
    node: ast.AST, carriers: set[str], modules: dict[str, str] | None = None
) -> set[str]:
    """Names a module binds at MODULE level from a carrier, keeping the command.

    `from .reader import PRE_PASS` then `COMMAND = PRE_PASS` re-exports the same
    value without an alias, so an import-only closure never reaches `COMMAND`
    and the module that runs it passes clean.

    Merely MENTIONING a carrier is not enough: `COUNT = len(PRE_PASS)` is an
    integer, and putting it in the package-wide set reds every downstream
    `print(COUNT)`. MODULES carries the qualified form, so
    `from . import bundle` then `COMMAND = bundle.PRE_PASS` re-exports too.
    """
    found: set[str] = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        targets, value = _assignment(child)
        if value is not None and _renames_a_carrier(value, carriers, modules):
            found |= {t.id for t in targets if isinstance(t, ast.Name)}
        found |= _module_level_rebinds(child, carriers, modules)
    return found


def _module_level_reads(node: ast.AST, wanted: frozenset[str]) -> set[str]:
    """Names bound from a WANTED read at MODULE level, function bodies excluded.

    A function-local binding cannot leave its module, so counting one would put
    a common local name — `argv` — in the package-wide carrier set and flag every
    unrelated parameter that shares it. Only a module-level name is importable,
    which is exactly the cross-module surface this set exists to describe.
    """
    found: set[str] = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        targets, value = _assignment(child)
        if value is not None and _env_read(value, wanted):
            found |= {t.id for t in targets if isinstance(t, ast.Name)}
        found |= _module_level_reads(child, wanted)
    return found


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


def _renames_a_carrier(
    value: ast.expr, carriers: set[str], modules: dict[str, str] | None = None
) -> bool:
    """Is VALUE the carrier itself under a new name, rather than something
    DERIVED from it?

    A rename is `CMD = PRE_PASS`, the module-qualified `CMD = bundle.PRE_PASS`,
    or the argv re-wrap `CMD = [*PRE_PASS, *args]`. Anything that merely MENTIONS
    a carrier produces a different value — `done = run_or_refuse(argv, ...)` is a
    CompletedProcess, and treating that as a command reds every
    `print(done.stdout)` in the package. The attribute form is admitted only
    through a MODULES alias, for the same reason.
    """
    if isinstance(value, ast.Attribute):
        return (
            getattr(value.value, "id", "") in (modules or {}) and value.attr in carriers
        )
    if isinstance(value, ast.Name):
        return value.id in carriers
    if isinstance(value, (ast.List, ast.Tuple)):
        return any(
            isinstance(element, ast.Starred)
            and _renames_a_carrier(element.value, carriers, modules)
            for element in value.elts
        )
    # `CMD = PRE_PASS + ["--flag"]` still runs the caller's executable, and so
    # does `["sudo"] + PRE_PASS` — the command survives the concatenation on
    # either side.
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        return _renames_a_carrier(value.left, carriers, modules) or _renames_a_carrier(
            value.right, carriers, modules
        )
    return False


def _local_aliases(
    tree: ast.AST, carriers: set[str], modules: dict[str, str] | None = None
) -> set[str]:
    """Names this module binds, at any scope, by RENAMING a carrier — to a fixed
    point, so a chain of renames stays one carrier.

    MODULE level only. A name a function binds lives in that function, so
    promoting it here would make an unrelated module-level `CMD = ["echo"]` the
    caller's command. `visit` collects each scope's own aliases as it enters it.
    """
    found = set(carriers)
    while True:
        grown = set(found)
        _collect_aliases(tree, grown, modules)
        if grown == found:
            return found - carriers
        found = grown


def _collect_aliases(
    node: ast.AST, grown: set[str], modules: dict[str, str] | None = None
) -> None:
    """Add every rename of a carrier in NODE's OWN scope to GROWN."""
    for child in _own_scope(node):
        targets, value = _assignment(child)
        if value is not None and _renames_a_carrier(value, grown, modules):
            grown |= {t.id for t in targets if isinstance(t, ast.Name)}


def _attr_carriers(
    tree: ast.AST, external: frozenset[tuple[str, str]], modules: dict[str, str]
) -> set[str]:
    """Carrier names this module reaches as `m.NAME` through a MODULES alias,
    matched against the module that actually defines the carrier."""
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and (modules.get(getattr(node.value, "id", ""), ""), node.attr) in external
    }


def _scope_imports(
    node: ast.AST, external: frozenset[tuple[str, str]]
) -> tuple[frozenset[str], frozenset[str]]:
    """(carrier names this scope imports, names its imports take over).

    A function-local `from .bundle import PRE_PASS` binds the carrier here and
    nowhere else; one from a module that does not define it binds an unrelated
    value under the same spelling, so the module-wide carrier must not answer
    for this scope. Collapsing both under the bound name keeps whichever the
    walk saw last.
    """
    package = package_modules()
    here: set[str] = set()
    taken: set[str] = set()
    for child in _own_scope(node):
        if not isinstance(child, ast.ImportFrom):
            continue
        parts = (child.module or "").split(".")
        for alias in child.names:
            bound = alias.asname or alias.name
            origin = (parts[-1], alias.name)
            carries = (child.level or parts[0] in package) and origin in external
            (here if carries else taken).add(bound)
    return frozenset(here), frozenset(taken)


def violations(
    text: str,
    wanted: frozenset[str] | None = None,
    external: frozenset[tuple[str, str]] = frozenset(),
) -> list[int]:
    """1-based lines that read a caller-supplied command unsafely.

    EXTERNAL is {@link command_names}' package-wide carrier set, as (defining
    module, name) pairs. A pair this module IMPORTS from that module is treated
    exactly like a name this module read, so the read and the run may live in
    different files.
    """
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
    direct, modules = _imports(tree, package_modules())
    # An imported carrier has no read line HERE, so it can only ever be reported
    # at the call that runs it — which is the line to rewrite anyway. Matched on
    # the SOURCE name, so an alias is the same carrier under another spelling.
    imported = (
        {bound for bound, origin in direct.items() if origin in external}
        | _attr_carriers(tree, external, modules)
    ) - set(reads)
    # A carrier keeps its status through a rename INSIDE this module, at any
    # scope: `CMD = PRE_PASS` then `subprocess.run(CMD)` is the same value under
    # a new name, and matching only read-or-imported names lets the rename walk
    # straight past the refusal.
    imported |= _local_aliases(tree, reads.keys() | imported, modules) - set(reads)
    carriers = reads.keys() | imported
    refusing = _refusing_helpers(tree)
    first_parameters = _first_parameters(tree)
    refused: set[str] = set()
    unguarded: dict[str, int] = {}
    inline: set[int] = set()

    def visit(
        node: ast.AST,
        shadowed: frozenset[str],
        scoped: frozenset[str],
        masked: frozenset[str],
    ) -> None:
        """Walk NODE, carrying the names a nearer binding has taken over.

        A carrier's name is a module-wide string, and Python is not: a parameter
        or a local assignment called `PRE_PASS` is a different value, so flagging
        a call inside that function refuses something the caller never supplied.
        A function-local IMPORT binds the same way, so one from a module that
        does not define the carrier takes the name over here — and one that does
        brings the carrier into this scope alone.
        """
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            shadowed = shadowed | _shadowed_in(node, carriers, wanted)
            here, taken = _scope_imports(node, external)
            shadowed = (shadowed | taken) - here
            # This scope's own renames belong to it: a `CMD = PRE_PASS` inside a
            # function must not make an unrelated module-level `CMD` a carrier.
            renamed = set((carriers | scoped | here) - shadowed - taken)
            _collect_aliases(node, renamed, modules)
            scoped = frozenset((scoped | here | renamed) - taken - carriers)
            # A nested `def` of a refusing helper's name is a DIFFERENT function,
            # so a call here reaches that one and the module-level refusal says
            # nothing about it.
            masked = masked | {
                child.name
                for child in _own_scope(node)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        if isinstance(node, ast.Call):
            module, attr = _called_name(node)
            runs_it = module == "subprocess" and attr in _SUBPROCESS_CALLS
            # An argv built in the call itself binds no name, so the read loop
            # never saw it — the plainest form of the defect this check is for.
            argv = _argv_of(node)
            if runs_it and argv is not None and _env_read(argv, wanted):
                inline.add(node.lineno)
            names = _argv_names(node, modules, first_parameters.get(attr)) & (
                (carriers | scoped) - shadowed
            )
            if names:
                if attr == _REFUSAL or (
                    not module and attr in refusing and attr not in masked
                ):
                    refused.update(names)
                elif runs_it or names & (imported | scoped):
                    for name in names:
                        unguarded.setdefault(name, node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, shadowed, scoped, masked)

    visit(tree, frozenset(), frozenset(), frozenset())
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
    external = command_names(wanted)
    paths = [str(p) for p in sorted(_PACKAGE.rglob("*.py"))] if not argv else argv
    sys.exit(
        run_line_checks(
            paths,
            lambda text: violations(text, wanted, external),
            "this reads a caller-supplied command but does not run it through "
            f"`_refusal.{_REFUSAL}` — a binary the runner cannot execute then "
            "RAISES past `check=False`, losing a resolution the model was "
            f"already billed for. Route it through `{_REFUSAL}`, or annotate "
            f"`# {_ALLOW}: <reason>`.",
        )
    )


if __name__ == "__main__":
    main(sys.argv[1:])
