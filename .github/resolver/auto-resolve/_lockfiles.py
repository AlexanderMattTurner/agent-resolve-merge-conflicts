"""PROBLEM CLASS — a lockfile merged as text is silent corruption: whatever a
line-merge or a model writes into a lockfile's conflicted region is a guess at
what the lock command would produce, never a fact about it. `resolve-generated.mjs`
plus `config/auto-resolve-regen-rules.json` already route the lockfiles a caller
declares a regeneration rule for; that caller's rule table always takes
precedence. This module is the FALLBACK for a lockfile a caller declared no rule
for: it recognizes the common ecosystems by basename, at any depth, and either
regenerates the lockfile from its manifest or fails loud — it never lets a
recognized lockfile fall through to a textual merge.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Mirrors resolve-generated.mjs's scrubbedEnv(): a GIT_CONFIG_VALUE_* entry can
# carry the push token as a base64 Authorization header, and a
# GITHUB_ENV/PATH/OUTPUT/STATE write from the derive command would let it inject
# BASH_ENV or a PATH prefix into the next step of the same job.
_SECRETISH = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|CREDENTIAL", re.IGNORECASE)
_GIT_CONFIG_INJECTION = re.compile(r"^GIT_CONFIG_(?:COUNT|KEY_\d+|VALUE_\d+)$")
_RUNNER_CHANNEL = re.compile(r"^GITHUB_(?:ENV|PATH|OUTPUT|STATE)$")


class LockfileError(Exception):
    """Raised when a recognized lockfile cannot be regenerated or verified."""


@dataclass(frozen=True)
class LockfileRule:
    basename: str
    tool: str
    manifest: str
    derive: tuple[str, ...] | Callable[[Path], tuple[str, ...]]
    check: tuple[str, ...] | None = None
    extra_env: Callable[[Path], dict[str, str]] | None = None
    co_outputs: tuple[str, ...] = ()
    manifest_ok: Callable[[Path], bool] | None = None


def _poetry_derive(directory: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["poetry", "--version"], cwd=directory, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise LockfileError(
            f"{directory}: `poetry --version` exited {result.returncode} — "
            f"cannot pick the right `poetry lock` flags:\n{result.stderr}"
        )
    match = re.search(r"(?P<major>\d+)\.\d+\.\d+", result.stdout)
    major = int(match.group("major")) if match else 0
    return ("lock",) if major >= 2 else ("lock", "--no-update")


def _poetry_manifest_ok(manifest: Path) -> bool:
    with manifest.open("rb") as fh:
        return "poetry" in tomllib.load(fh).get("tool", {})


def _yarn_is_berry(directory: Path) -> bool:
    if (directory / ".yarnrc.yml").exists():
        return True
    package_json = directory / "package.json"
    if not package_json.exists():
        return False
    data = json.loads(package_json.read_text(encoding="utf-8"))
    match = re.match(r"yarn@(?P<major>\d+)", data.get("packageManager", ""))
    return bool(match) and int(match.group("major")) >= 2


def _yarn_derive(directory: Path) -> tuple[str, ...]:
    if _yarn_is_berry(directory):
        return ("install", "--mode=update-lockfile")
    return ("install", "--ignore-scripts")


def _yarn_env(directory: Path) -> dict[str, str]:
    return {"YARN_ENABLE_SCRIPTS": "0"} if _yarn_is_berry(directory) else {}


LOCKFILE_RULES: tuple[LockfileRule, ...] = (
    LockfileRule(
        basename="uv.lock",
        tool="uv",
        manifest="pyproject.toml",
        derive=("lock",),
        check=("lock", "--check"),
    ),
    LockfileRule(
        basename="poetry.lock",
        tool="poetry",
        manifest="pyproject.toml",
        derive=_poetry_derive,
        check=("check", "--lock"),
        manifest_ok=_poetry_manifest_ok,
    ),
    LockfileRule(
        basename="package-lock.json",
        tool="npm",
        manifest="package.json",
        derive=("install", "--package-lock-only", "--ignore-scripts"),
    ),
    LockfileRule(
        basename="pnpm-lock.yaml",
        tool="pnpm",
        manifest="package.json",
        derive=("install", "--lockfile-only", "--ignore-scripts"),
    ),
    LockfileRule(
        basename="yarn.lock",
        tool="yarn",
        manifest="package.json",
        derive=_yarn_derive,
        extra_env=_yarn_env,
    ),
    LockfileRule(
        basename="Cargo.lock",
        tool="cargo",
        manifest="Cargo.toml",
        derive=("update", "--workspace"),
        check=("metadata", "--locked", "--format-version", "1"),
    ),
    LockfileRule(
        basename="go.sum",
        tool="go",
        manifest="go.mod",
        derive=("mod", "tidy"),
        check=("mod", "verify"),
        co_outputs=("go.mod",),
    ),
)

_RULES_BY_BASENAME = {rule.basename: rule for rule in LOCKFILE_RULES}

# The shared marker pattern (`_marker_verdict.py`'s own copy, restated rather
# than imported: that module pulls in bundle.py's dependencies, and this one
# ships to the land job as a bare stdlib script). Matches the four spellings a
# real conflict or a hand-authored splice can leave in a lockfile.
_CONFLICT_MARKER_RE = re.compile(r"^(?:<{7}|={7}|>{7}|\|{7})(?: |$)", re.MULTILINE)


def is_caller_owned(path: str, owned: set[str]) -> bool:
    """Whether PATH is owned by the calling repository's own rule table.

    `owned` is `resolve-generated.mjs --owned`'s output: exact paths AND
    directory-prefix entries ending in `/` (its own rule schema names
    `ownsPrefix`). Exact equality alone misses a lockfile under an owned
    subtree, which would then be regenerated here with the WRONG command
    instead of the caller's — the one thing this module's header promises
    never happens."""
    return path in owned or any(p.endswith("/") and path.startswith(p) for p in owned)


def rule_for(path: str) -> LockfileRule | None:
    """The rule for `path`'s basename, or None. Basename-only, at any depth: the
    recognized set must never depend on the filesystem, so every router (this
    module, the caller's rule table, a human reading a diff) agrees on it even
    when the manifest that would make it derivable is missing."""
    return _RULES_BY_BASENAME.get(Path(path).name)


def derivable(path: str, root: str) -> bool:
    """Whether `path`'s manifest marker sits beside it under `root`."""
    rule = rule_for(path)
    if rule is None:
        return False
    manifest = (Path(root) / path).parent / rule.manifest
    if not manifest.exists():
        return False
    return rule.manifest_ok is None or rule.manifest_ok(manifest)


def scrubbed_env() -> dict[str, str]:
    """The subprocess environment for a derive/check command. A lockfile's
    derive command runs build backends the repo's own manifest names (PEP 517
    hooks, npm/yarn lifecycle scripts), and nothing suppresses those the way
    --ignore-scripts suppresses npm's. Strip anything that could hand the
    command a credential or let it inject into the runner's channels — see
    resolve-generated.mjs's scrubbedEnv() for the same reasoning."""
    return {
        k: v
        for k, v in os.environ.items()
        if not (
            _SECRETISH.search(k)
            or _GIT_CONFIG_INJECTION.match(k)
            or _RUNNER_CHANNEL.match(k)
        )
    }


def _derive_argv(rule: LockfileRule, directory: Path) -> tuple[str, ...]:
    return rule.derive(directory) if callable(rule.derive) else rule.derive


def _run(
    rule: LockfileRule, argv: tuple[str, ...], directory: Path, env: dict[str, str]
) -> None:
    result = subprocess.run(
        [rule.tool, *argv], cwd=directory, env=env, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise LockfileError(
            f"{directory / rule.basename}: `{rule.tool} {' '.join(argv)}` "
            f"exited {result.returncode} — resolve the lockfile by fixing the "
            f"manifest, not by editing it:\n{result.stderr}"
        )


def _seed_bytes(seed_ref: str | None, path: str, root: str) -> bytes | None:
    """PATH's bytes at SEED_REF (the merge base, when the caller has one), or
    None when there is nothing to seed from: no ref given, the ref predates the
    path (a brand-new lockfile), or `root` is not the git checkout `seed_ref`
    lives in."""
    if not seed_ref:
        return None
    result = subprocess.run(
        ["git", "show", f"{seed_ref}:{path}"], cwd=root, capture_output=True
    )
    return result.stdout if result.returncode == 0 else None


def _clear_if_conflicted(
    lockfile: Path, path: str, root: str, seed_ref: str | None
) -> None:
    """Reseed LOCKFILE when it still carries a real merge's conflict markers.

    A lockfile routed here for its manifest being clean can still be marker-laden
    itself — a real conflict, not the auto-merged-clean shape #4585 fixed. Every
    derive command reads the existing lockfile as a hint (JSON/TOML/its own
    format), so marker text makes it fail to PARSE rather than fail to LOCK,
    misreporting a real conflict as `refused`.

    Restoring SEED_REF's bytes (the merge base) rather than deleting the file
    is what keeps the derive command a MINIMAL relock: every lock tool here
    treats an existing lockfile as a hint and only changes what the manifest
    diff forces, so a seeded run touches just the packages the merge actually
    bumped. An empty file gives it no hint, so it re-resolves every transitive
    dependency to today's newest compatible version — the drift a merge that
    only bumped one package must never carry. Falling back to delete (no seed,
    or the ref has no such path) is still safe: every rule here can construct
    a lockfile from nothing but the manifest, at the cost of that drift."""
    if not lockfile.is_file():
        return
    try:
        text = lockfile.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return
    if not _CONFLICT_MARKER_RE.search(text):
        return
    seed = _seed_bytes(seed_ref, path, root)
    if seed is not None:
        lockfile.write_bytes(seed)
        return
    lockfile.unlink()


def regenerate(path: str, root: str, seed_ref: str | None = None) -> list[str]:
    """Regenerate `path` from its manifest and verify the result. Returns every
    path this touched — the lockfile plus any declared `co_outputs` (`go.sum`'s
    generator legitimately rewrites `go.mod` too) — for the caller to stage.

    `seed_ref` is the merge base commit, when the caller has one: it is what
    a conflicted lockfile is restored from before the derive command runs,
    so the relock only picks up what the manifests actually changed. See
    `_clear_if_conflicted` for why an unseeded relock drifts.

    Raises `LockfileError` for anything short of a verified regeneration: no
    rule, no manifest beside it, the tool missing from PATH, a failing derive
    or check, or — for a rule with no dedicated check command — a derive that
    turns out not to be idempotent."""
    rule = rule_for(path)
    if rule is None:
        raise LockfileError(f"{path}: not a recognized lockfile")
    if not derivable(path, root):
        raise LockfileError(
            f"{path}: no {rule.manifest} beside it under {root} — cannot regenerate"
        )
    if shutil.which(rule.tool) is None:
        raise LockfileError(
            f"{path}: `{rule.tool}` is not on PATH — install it to resolve this lockfile"
        )

    lockfile = Path(root) / path
    directory = lockfile.parent
    _clear_if_conflicted(lockfile, path, root, seed_ref)
    env = {**scrubbed_env(), **(rule.extra_env(directory) if rule.extra_env else {})}
    derive_argv = _derive_argv(rule, directory)
    touched = [path, *(str(Path(path).parent / co) for co in rule.co_outputs)]

    _run(rule, derive_argv, directory, env)

    if rule.check is not None:
        _run(rule, rule.check, directory, env)
        return touched

    before = lockfile.read_bytes()
    _run(rule, _derive_argv(rule, directory), directory, env)
    after = lockfile.read_bytes()
    if before != after:
        # Restore the first run's bytes: a rule with no `check` command has no
        # other way to prove which run (if either) is trustworthy, so the
        # refusal below must not leave the second run's bytes on disk for a
        # later `git add -A` to stage.
        lockfile.write_bytes(before)
        raise LockfileError(
            f"{path}: `{rule.tool} {' '.join(derive_argv)}` is not idempotent — "
            "two consecutive runs produced different bytes, so the regenerated "
            "lockfile cannot be trusted"
        )
    return touched


def _one_line(text: str) -> str:
    """TEXT with every newline and tab folded to a space.

    A verdict line is TAB-separated and newline-terminated, and a failing tool's
    own output ends up inside a reason. Left raw, one `uv` warning line becomes a
    line the caller reads as a verdict for an empty path."""
    return " ".join(text.split())


def _route_one(
    path: str,
    root: str,
    owned: set[str],
    conflicted_manifests: set[str],
    seed_ref: str | None = None,
) -> str | None:
    if is_caller_owned(path, owned):
        return f"caller-owned\t{path}"
    rule = rule_for(path)
    if rule is None:
        return None
    if not derivable(path, root):
        return f"refused\t{path}\tno {rule.manifest} beside it to regenerate from"
    manifest_path = str((Path(path).parent / rule.manifest))
    if manifest_path in conflicted_manifests:
        return f"deferred\t{path}"
    try:
        touched = regenerate(path, root, seed_ref)
    except LockfileError as exc:
        return f"refused\t{path}\t{_one_line(str(exc))}"
    return f"regenerated\t{path}\t{' '.join(touched)}"


def main(argv: list[str] | None = None) -> None:
    """Two modes, mutually exclusive:

    `--route --root <dir> [--owned-file <file>] [--manifest-conflicted <path>
    ...] [--seed-ref <commit>] -- <path>...` prints one TAB-separated verdict
    line per recognized path, for a bash caller to read. `--seed-ref` is the
    merge base commit; a conflicted lockfile is restored from it before
    relocking so the relock stays minimal (see `_clear_if_conflicted`).
    An unrecognized path prints nothing. Exit
    status is 0 whenever routing itself ran — a `refused` line is data the
    caller acts on, not a crash.

    `--recognize -- <path>...` prints the recognized paths, one per line, with
    NO filesystem or tool access — basename matching only. This is the mode a
    fork-head run uses: recognizing a lockfile is safe on untrusted content,
    but regenerating one runs build backends that head's author wrote."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--route", action="store_true")
    mode.add_argument("--recognize", action="store_true")
    parser.add_argument("--root")
    parser.add_argument("--owned-file", default=None)
    parser.add_argument("--manifest-conflicted", action="append", default=[])
    parser.add_argument("--seed-ref", default=None)
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)

    if args.recognize:
        for path in args.paths:
            if rule_for(path) is not None:
                print(path)
        return

    if not args.root:
        parser.error("--route requires --root")
    owned: set[str] = set()
    if args.owned_file:
        owned = {
            line.strip()
            for line in Path(args.owned_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    conflicted_manifests = set(args.manifest_conflicted)

    for path in args.paths:
        line = _route_one(path, args.root, owned, conflicted_manifests, args.seed_ref)
        if line is not None:
            print(line)


if __name__ == "__main__":
    main(sys.argv[1:])
