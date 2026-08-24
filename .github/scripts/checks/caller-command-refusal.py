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
from typing import NamedTuple
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
# The module that DEFINES the refusal. A call clears a carrier only when it
# reaches this one: `thirdparty.run_or_refuse(argv)` shares the spelling and
# runs the command anyway, so matching the name alone fails open.
_REFUSAL_MODULE = "_refusal"
# A handoff target that is settled, so no name can collide with it: a real
# refusal, and a module-qualified callee whose definition this module cannot
# see. Neither is ever a key in the refusing set, which holds def names.
_REFUSED = "\0refused"
_FOREIGN = "\0foreign"
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
    which is why the answer is a fixed point grown from the real refusals:
    `execute` calling `unsafe(argv)` refuses nothing however many
    `run_or_refuse` calls its other branch holds, and a recursive pair that
    reaches no refusal at all cannot certify itself.
    """
    handoffs = {}
    parameters = _first_parameters(tree)
    _, mods = _imports(tree, package_modules())
    rebound = _refusal_is_rebound(tree)
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = _first_parameter(node)
        if first is None:
            continue
        targets = _parameter_handoffs(node, first, parameters, rebound, mods)
        # A name DEFINED TWICE reaches whichever definition ran last before the
        # call, so a raw one followed by a safe one must not clear the calls
        # between them. Merging every definition's targets refuses the name
        # only when they all do.
        if node.name in handoffs:
            earlier = handoffs[node.name]
            targets = None if earlier is None or targets is None else earlier + targets
        handoffs[node.name] = targets
    # LEAST fixed point: nothing refuses until a real `run_or_refuse` proves it,
    # and each round adds the helpers whose every target is already proven. The
    # greatest one certifies `a` calling `b` and `b` calling `a` with neither
    # reaching a refusal. A helper the parameter reaches no call from never
    # enters either way: `all()` over its empty target list would say otherwise.
    refusing: set[str] = set()
    candidates = {name: targets for name, targets in handoffs.items() if targets}
    while True:
        grown = refusing | {
            name
            for name, targets in candidates.items()
            if all(target == _REFUSED or target in refusing for target in targets)
        }
        if grown == refusing:
            return frozenset(refusing)
        refusing = grown


def _refusal_is_rebound(node: ast.AST, inherited: bool = False) -> bool:
    """Does NODE's OWN scope bind the bare `run_or_refuse` to anything other
        than `_refusal`? INHERITED is the answer the enclosing scope reached.

    EVERY binding of the name counts — an import from another module, a local
        `def`, a PARAMETER, an assignment. A callback the caller supplied is not
        this package's refusal, and `def f(run_or_refuse): run_or_refuse(argv)`
        runs whatever it was handed. The call then falls through to the
        refusing-helper set, which decides by what the binding does rather than by
        its name. `_refusal` itself defines the real one, so its own definition
        needs the opt-out comment.

        Read from the imports directly rather than from {@link _imports}, which
        keeps only package modules: `from thirdparty import run_or_refuse` is
        exactly the binding this must see, and that map drops it.

        Per SCOPE, like every other binding here. A function-local
        `from thirdparty import run_or_refuse` is the nearer binding, so a module
        that imports the real one still calls the third-party function in there.
    """
    arguments = getattr(node, "args", None)
    if isinstance(arguments, ast.arguments) and _REFUSAL in {
        arg.arg
        for group in (
            arguments.posonlyargs,
            arguments.args,
            arguments.kwonlyargs,
            [arguments.vararg] if arguments.vararg else [],
            [arguments.kwarg] if arguments.kwarg else [],
        )
        for arg in group
    }:
        return True
    answer = inherited
    for child in _own_scope(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if child.name == _REFUSAL:
                answer = True
            continue
        targets, value = _assignment(child)
        if value is not None and any(
            isinstance(t, ast.Name) and t.id == _REFUSAL for t in targets
        ):
            answer = True
            continue
        if not isinstance(child, ast.ImportFrom):
            continue
        for alias in child.names:
            if (alias.asname or alias.name) == _REFUSAL:
                answer = (child.module or "").split(".")[-1] != _REFUSAL_MODULE
    return answer


def _is_the_refusal(
    module: str, attr: str, rebound: bool, mods: dict[str, str]
) -> bool:
    """Does this call reach THIS package's `_refusal.run_or_refuse`?

    A qualified call must go through a module alias for `_refusal`. A bare one
    is the refusal unless the nearest binding of the name is something else —
    an import from another module, or a local `def`.
    """
    if attr != _REFUSAL:
        return False
    if module:
        return mods.get(module) == _REFUSAL_MODULE
    return not rebound


def _parameter_handoffs(
    node: ast.AST,
    first: str,
    parameters: dict[str, str] | None = None,
    rebound: bool = False,
    mods: dict[str, str] | None = None,
) -> list[str] | None:
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
        # The CALLEE's own parameter name, so `unsafe(command=argv)` is the
        # handoff it plainly is rather than a call carrying no command.
        argv = _argv_of(call, (parameters or {}).get(attr))
        if argv is None or not any(
            isinstance(sub, ast.Name) and sub.id in held for sub in ast.walk(argv)
        ):
            continue
        if module == "subprocess" and attr in _SUBPROCESS_CALLS:
            return None
        if _is_the_refusal(module, attr, rebound, mods or {}):
            targets.append(_REFUSED)
        else:
            targets.append(_FOREIGN if module else attr)
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


def _first_parameter(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The parameter a helper's refusal is about: its first, in declaration
    order. KEYWORD-ONLY counts — `def execute(*, argv)` takes its command
    exactly as `def execute(argv)` does, and a helper with no positional
    parameter at all would otherwise be invisible to both the refusal trace and
    the call scan, so `execute(argv=PRE_PASS)` ran the carrier unflagged.

    Any parameter after the first is not covered: a carrier reaching one is
    `elsewhere` in {@link violations}, which flags. That is the safe direction
    when declaration order does not say which parameter is the command.
    """
    declared = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    return declared[0].arg if declared else None


def _first_parameters(tree: ast.AST) -> dict[str, str]:
    """{function name: {@link _first_parameter}}, for the keyword form of a
    handoff. Module-local, like {@link _refusing_helpers}: a helper defined
    elsewhere is read when that module is checked."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = _first_parameter(node)
        if first is not None:
            out.setdefault(node.name, first)
    return out


def _own_scope(node: ast.AST) -> list[ast.AST]:
    """NODE's descendants, stopping at every nested scope boundary.

    `ast.walk` crosses into a nested function, so an assignment there would read
    as a rebinding out here — and a name the enclosing scope still resolves to
    the module-level carrier would drop out of the carrier set.

    SOURCE order. Every caller here resolves a repeated binding by position —
    the last import of a name wins, the first read is where the value came in,
    the first rebinding is where a class body takes the name over — so handing
    them the walk's own order silently answered each of those backwards.
    """
    out: list[ast.AST] = []
    stack = list(reversed(list(ast.iter_child_nodes(node))))
    while stack:
        child = stack.pop()
        out.append(child)
        if isinstance(child, _SCOPES):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(child))))
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

    A `global` or `nonlocal` declaration is the other carve-out: the assignment
    after it writes THROUGH to the outer binding rather than creating a local
    one, so the name still resolves to the carrier and a raw run of it here is
    the call this check exists for.
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
    declared = {
        name
        for child in _own_scope(node)
        if isinstance(child, (ast.Global, ast.Nonlocal))
        for name in child.names
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
    return frozenset((taken - declared) & carriers)


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
    return set() if argv is None else _plain_names(argv, modules or {})


def _plain_names(node: ast.expr, modules: dict[str, str]) -> set[str]:
    """Every carrier-shaped name in NODE: a bare name, or an attribute reached
    through a MODULES alias, which is the same value under another spelling."""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif (
            isinstance(child, ast.Attribute)
            and getattr(child.value, "id", "") in modules
        ):
            found.add(child.attr)
    return found


def _other_argument_names(
    call: ast.Call, modules: dict[str, str], parameter: str | None = None
) -> set[str]:
    """The plain names in every argument of CALL EXCEPT its argv.

    A helper is free to take the command second: `execute("label", PRE_PASS)`
    hands the carrier over as plainly as the first-argument form, and reading
    argv alone sees no command in either the caller or the helper. PARAMETER
    names the helper's own argv keyword, so `execute(argv=PRE_PASS)` is the argv
    here exactly as it is to the scan that reads it.
    """
    argv = _argv_of(call, parameter)
    found: set[str] = set()
    for node in [*call.args, *(kw.value for kw in call.keywords)]:
        if node is argv:
            continue
        found |= _plain_names(node, modules)
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

    MODULE level only. A function-local import binds in that function, so
    collecting it here would make an unrelated module-level name of the same
    spelling a carrier. `_scope_imports` introduces those where they bind.
    """
    direct: dict[str, tuple[str, str]] = {}
    modules: dict[str, str] = {}
    for node in _own_scope(tree):
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


def _alias_sources(
    node: ast.AST, modules: dict[str, str]
) -> dict[tuple[int, str], str]:
    """{(scope, renamed name): the name it renames}, over every scope.

    `cmd = argv` then `run_or_refuse(cmd)` refuses the value `argv` holds, so
    the refusal has to reach `argv`'s READ. Keyed on the new spelling alone,
    it cleared nothing and the safe function was reported at its read line.

    By BINDING, like the reads and the refusals: two functions may each alias
    their own read to `cmd`, and one module-wide entry would map one function's
    refusal onto the other function's name — leaving a safe read unrefused.
    """
    out: dict[tuple[int, str], str] = {}
    stack = [(0, node)]
    while stack:
        scope, current = stack.pop()
        for child in _own_scope(current):
            if isinstance(child, _SCOPES):
                stack.append((id(child), child))
            targets, value = _assignment(child)
            if value is None:
                continue
            sources = sorted(_plain_names(value, modules))
            if len(sources) != 1:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id != sources[0]:
                    out.setdefault((scope, target.id), sources[0])
    return out


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


class ScopeImports(NamedTuple):
    """What one scope's own imports bind: `carries` are carrier names it brings
    in, `takes` are names its imports take over from an outer binding, and
    `modules` are the module aliases a qualified carrier is reached through."""

    carries: frozenset[str]
    takes: frozenset[str]
    modules: dict[str, str]


def _scope_imports(node: ast.AST, external: frozenset[tuple[str, str]]) -> ScopeImports:
    """What NODE's own imports bind — see {@link ScopeImports}.

    A function-local `from .bundle import PRE_PASS` binds the carrier here and
    nowhere else; one from a module that does not define it binds an unrelated
    value under the same spelling, so the module-wide carrier must not answer
    for this scope. Collapsing both under the bound name keeps whichever the
    walk saw last.

    A local `from . import bundle` binds a MODULE, and dropping it would leave
    `bundle.PRE_PASS` in that scope reaching no carrier at all.
    """
    package = package_modules()
    # {bound name: does this import bring the carrier?} — one entry per name, so
    # a scope importing the same spelling twice keeps only the LAST answer.
    # Adding to both sets instead left the earlier classification standing, and
    # the caller subtracts `taken`, so a carrier imported second read as taken.
    brings: dict[str, bool] = {}
    modules: dict[str, str] = {}
    for child in _own_scope(node):
        if isinstance(child, ast.Import):
            for alias in child.names:
                root = alias.name.split(".")[0]
                if root in package:
                    modules[(alias.asname or alias.name).split(".")[0]] = root
            continue
        if not isinstance(child, ast.ImportFrom):
            continue
        parts = (child.module or "").split(".")
        for alias in child.names:
            bound = alias.asname or alias.name
            if child.level and child.module is None:
                modules[bound] = alias.name
                continue
            origin = (parts[-1], alias.name)
            brings[bound] = bool(
                (child.level or parts[0] in package) and origin in external
            )
    return ScopeImports(
        frozenset(name for name, carries in brings.items() if carries),
        frozenset(name for name, carries in brings.items() if not carries),
        modules,
    )


def _shadow_lines(
    node: ast.AST, carriers: set[str], wanted: frozenset[str]
) -> dict[str, int]:
    """{name: the line a CLASS body takes it over on}.

    A class body runs statement by statement, so a name a later line rebinds
    still resolves to the module binding above it. Shadowing the whole body
    treats a raw run before the rebinding as reaching a different value.
    """
    taken = _shadowed_in(node, carriers, wanted)
    out: dict[str, int] = {}
    for child in _own_scope(node):
        targets, _ = _assignment(child)
        for target in targets:
            if isinstance(target, ast.Name) and target.id in taken:
                out.setdefault(target.id, child.lineno)
    return out


def _import_lines(node: ast.AST, taken: frozenset[str]) -> dict[str, int]:
    """{name: the line an import in NODE's OWN scope takes it over on}.

    A class body's imports bind in statement order like its assignments, so a
    call above one still reaches the module binding.
    """
    out: dict[str, int] = {}
    for child in _own_scope(node):
        if not isinstance(child, ast.ImportFrom):
            continue
        for alias in child.names:
            bound = alias.asname or alias.name
            if bound in taken:
                out.setdefault(bound, child.lineno)
    return out


def _scope_reads(node: ast.AST, wanted: frozenset[str]) -> dict[str, int]:
    """{name: line} for every caller-command read in NODE's OWN scope."""
    found: dict[str, int] = {}
    for child in _own_scope(node):
        targets, value = _assignment(child)
        if value is None or not _env_read(value, wanted):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                found.setdefault(target.id, child.lineno)
    return found


def _reads_by_scope(
    node: ast.AST, wanted: frozenset[str]
) -> list[tuple[int, dict[str, int]]]:
    """Every scope's own reads, innermost last — the flat name set the carrier
    matching still works from."""
    out = [(id(node), _scope_reads(node, wanted))]
    for child in _own_scope(node):
        if isinstance(child, _SCOPES):
            out += _reads_by_scope(child, wanted)
    return out


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
    reads = {name for scope in _reads_by_scope(tree, wanted) for name in scope[1]}
    direct, modules = _imports(tree, package_modules())
    # An imported carrier has no read line HERE, so it can only ever be reported
    # at the call that runs it — which is the line to rewrite anyway. Matched on
    # the SOURCE name, so an alias is the same carrier under another spelling.
    imported = (
        {bound for bound, origin in direct.items() if origin in external}
        | _attr_carriers(tree, external, modules)
    ) - reads
    # A carrier keeps its status through a rename INSIDE this module, at any
    # scope: `CMD = PRE_PASS` then `subprocess.run(CMD)` is the same value under
    # a new name, and matching only read-or-imported names lets the rename walk
    # straight past the refusal.
    imported |= _local_aliases(tree, reads | imported, modules) - reads
    carriers = reads | imported
    aliased = _alias_sources(tree, modules)
    refusing = _refusing_helpers(tree)
    first_parameters = _first_parameters(tree)
    # Keyed by BINDING, not by name: two functions can read the same env var
    # into the same local spelling, and one of them refusing it says nothing
    # about the other. A name resolves to the nearest scope that read it, which
    # is how Python resolves it.
    read_lines: dict[tuple[int, str], int] = {}
    refused: set[tuple[int, str]] = set()
    unguarded: dict[tuple[int, str], int] = {}
    inline: set[int] = set()

    def visit(
        node: ast.AST,
        shadowed: frozenset[str],
        scoped: frozenset[str],
        masked: frozenset[str],
        mods: dict[str, str],
        chain: tuple[tuple[int, dict[str, int]], ...],
        pending: dict[str, int],
        rebound: bool,
    ) -> None:
        """Walk NODE, carrying the names a nearer binding has taken over.

        A carrier's name is a module-wide string, and Python is not: a parameter
        or a local assignment called `PRE_PASS` is a different value, so flagging
        a call inside that function refuses something the caller never supplied.
        A function-local IMPORT binds the same way, so one from a module that
        does not define the carrier takes the name over here — and one that does
        brings the carrier into this scope alone.
        """
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            # A class body binds in statement order, so its rebindings take
            # effect from their own line down. A function body binds for all of
            # itself, and it cannot see a class body's names at all.
            if isinstance(node, ast.ClassDef):
                pending = {**pending, **_shadow_lines(node, carriers, wanted)}
            else:
                shadowed = shadowed | _shadowed_in(node, carriers, wanted)
                pending = {}
            rebound = _refusal_is_rebound(node, rebound)
            here, taken, local_modules = _scope_imports(node, external)
            mods = {**mods, **local_modules}
            if isinstance(node, ast.ClassDef):
                # Its imports are line-gated too. `here` is not: it only ADDS a
                # carrier, so gating it would flag a call the name reaches
                # nothing at yet.
                pending = {**pending, **_import_lines(node, taken)}
                shadowed = shadowed - here
            else:
                shadowed = (shadowed | taken) - here
            # This scope's own renames belong to it: a `CMD = PRE_PASS` inside a
            # function must not make an unrelated module-level `CMD` a carrier.
            renamed = set(
                (carriers | scoped | here | _attr_carriers(node, external, mods))
                - shadowed
                - taken
            )
            _collect_aliases(node, renamed, mods)
            scoped = frozenset((scoped | here | renamed) - taken - carriers)
            # A nested `def` of a refusing helper's name is a DIFFERENT function,
            # so a call here reaches that one and the module-level refusal says
            # nothing about it.
            masked = masked | {
                child.name
                for child in _own_scope(node)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            own = _scope_reads(node, wanted)
            chain = ((id(node), own), *chain)
            for name, lineno in own.items():
                read_lines[(id(node), name)] = lineno
        if isinstance(node, ast.Call):
            module, attr = _called_name(node)
            runs_it = module == "subprocess" and attr in _SUBPROCESS_CALLS
            # An argv built in the call itself binds no name, so the read loop
            # never saw it — the plainest form of the defect this check is for.
            argv = _argv_of(node)
            if runs_it and argv is not None and _env_read(argv, wanted):
                inline.add(node.lineno)
            here = shadowed | {
                name for name, line in pending.items() if line <= node.lineno
            }
            reach = (carriers | scoped | _attr_carriers(node, external, mods)) - here
            names = _argv_names(node, mods, first_parameters.get(attr)) & reach
            # A carrier the callee does not take as its ARGV is still handed
            # over, and a helper's refusal is only ever about its first
            # parameter — so a carrier in any later argument is unguarded.
            elsewhere = (
                _other_argument_names(node, mods, first_parameters.get(attr)) & reach
                if attr in first_parameters
                else set()
            )

            def binding(name):
                """(scope, name) for the nearest scope that read NAME, module
                scope otherwise — the binding this call actually reaches.

                A name no scope read may be a RENAME of one that was, so the
                walk follows the rename back: refusing `cmd` refuses the read
                `argv` it copied.
                """
                seen: set[str] = set()
                while name not in seen:
                    hit = next(((sid, name) for sid, own in chain if name in own), None)
                    if hit is not None:
                        return hit
                    seen.add(name)
                    source = next(
                        (
                            aliased[(sid, name)]
                            for sid, _ in chain
                            if (sid, name) in aliased
                        ),
                        None,
                    )
                    if source is None:
                        break
                    name = source
                return (0, name)

            for name in sorted(elsewhere):
                unguarded.setdefault(binding(name), node.lineno)
            if names:
                if _is_the_refusal(module, attr, rebound, mods) or (
                    not module and attr in refusing and attr not in masked
                ):
                    refused.update(binding(name) for name in names)
                elif runs_it or names & (imported | scoped):
                    for name in names:
                        unguarded.setdefault(binding(name), node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, shadowed, scoped, masked, mods, chain, pending, rebound)

    module_reads = _scope_reads(tree, wanted)
    for name, lineno in module_reads.items():
        read_lines[(0, name)] = lineno
    visit(
        tree,
        frozenset(),
        frozenset(),
        frozenset(),
        modules,
        ((0, module_reads),),
        {},
        _refusal_is_rebound(tree),
    )
    # The subprocess line wins when a binding is both unguarded and never
    # refused: it is the call to rewrite, where the read is only where the value
    # came from.
    hits = {
        **{key: ln for key, ln in read_lines.items() if key not in refused},
        **unguarded,
    }
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
