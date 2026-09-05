"""Print the pinned specs for the Python packages the auto-resolve job installs into
its ambient interpreter, one per line, read out of a pyproject.toml.

Two sets, because they sit in different tables and serve different steps (see
install-hook-tools.sh). Default: the distributions the `language: system` pre-commit
hooks import, from the dev extra. `--runtime`: the distributions the job's own
scripts import, from `[project].dependencies`.

They are named here by DISTRIBUTION name — `pyyaml` imports as `yaml` — while the
installer asserts the IMPORT names, so the two halves of the provisioning check each
other.

Reading the specs rather than restating them keeps pyproject.toml the one place these
versions live; a copy here would be a pin to keep in lockstep.
"""

import re
import sys
import tomllib

# Every third-party module imported by a .github/scripts hook that pre-commit runs
# with `language: system`, as its distribution name. A hook whose import is missing
# does not report a violation — it aborts with a traceback the resolver reads as a
# failed resolution, which is what this list exists to prevent.
#
# The list is literal because this module runs BEFORE its own dependencies are
# installed, so it cannot parse .pre-commit-config.yaml to derive it. Nothing
# derives it: the hooks that run belong to the CALLER, so a name is added here when
# a caller's hook first needs it. A name absent here is never installed, so the
# installer's import check skips it: the hook's own ModuleNotFoundError reports.
#
# The five grammar wheels after the first three are loaded through
# `importlib.import_module`, one per language a whole-tree comment scan meets. An import
# walk cannot see a dynamic import, so the derivation above never names them: a caller
# whose hook reads a `.yaml`, `.toml`, `.ts`, `.c` or `.rb` comment needs them here.
WANTED = frozenset(
    {
        "tree-sitter",
        "tree-sitter-bash",
        "tree-sitter-javascript",
        "tree-sitter-c",
        "tree-sitter-ruby",
        "tree-sitter-toml",
        "tree-sitter-typescript",
        "tree-sitter-yaml",
        "pyyaml",
        "pathspec",
        # A whole-tree comment scan reads Dockerfiles through dockerfile-parse, which
        # is not a grammar wheel and so is not loaded dynamically like the five above.
        "dockerfile-parse",
    }
)

# Runtime distributions the job's own steps import. `agent-sanitizer` is the redaction
# engine `bin/lib/transcript-publish.py` needs: absent, the log-staging step publishes
# a REDACTION-FAILED placeholder in place of the per-shard fan-out logs and stays
# green, so the run that most needs those logs is the one that loses them.
RUNTIME_WANTED = frozenset({"agent-sanitizer"})


def _canonical(spec: str) -> str:
    """SPEC's PEP 503 canonical distribution name, with any version/extras stripped.

    PROBLEM CLASS — which distribution does a requirement string name? Every
    consumer asks this, and a second implementation drifts on the shapes it did
    not think of: `pyyaml >= 6.0.3` is legal, and a copy that forgets the space
    answers `pyyaml ` and matches nothing. `--canonical` is how a caller outside
    Python reaches this one.
    """
    raw = re.split(r"[=<>!~\[]", spec, maxsplit=1)[0].strip()
    # PEP 503 normalization, not just lowercasing: `tree_sitter`, `tree.sitter` and
    # `tree-sitter` are one distribution and pip accepts all three, so matching the
    # literal text would read a legal respelling of a pin as a dropped one.
    return re.sub(r"[-_.]+", "-", raw.lower())


def _select(deps: list[str], wanted: frozenset[str], source: str) -> list[str]:
    """The WANTED entries of DEPS, sorted by distribution name.

    An absent name is REPORTED, not fatal. WANTED is the union over every caller's
    hooks, and the resolver now runs for repositories whose hook sets it does not
    know — so a caller that uses no pathspec-backed hook simply does not pin
    pathspec, and refusing there would make the resolver unusable rather than
    catching a dropped pin. The dropped-pin case still surfaces: the hook that
    needed it fails by name inside the run, and the caller's own suite is where
    the pin is asserted.
    """
    found = {_canonical(s): s for s in deps if _canonical(s) in wanted}
    missing = wanted - found.keys()
    if missing:
        print(
            f"hook-py-specs: {source} pins none of {sorted(missing)}; "
            "installing only what it does pin. A hook that needs one of these will "
            "fail by name.",
            file=sys.stderr,
        )
    return [found[name] for name in sorted(found)]


def dev_specs(pyproject: str) -> list[str]:
    """The `WANTED` entries of PYPROJECT's dev extra, sorted by distribution name."""
    with open(pyproject, "rb") as f:
        dev = tomllib.load(f)["project"]["optional-dependencies"]["dev"]
    return _select(dev, WANTED, f"{pyproject}'s dev extra")


def runtime_specs(pyproject: str) -> list[str]:
    """The `RUNTIME_WANTED` entries of PYPROJECT's `[project].dependencies`.

    A caller with no `[project].dependencies` table declares no runtime
    distributions, which is a legal shape rather than an error — this resolver
    runs for repositories that publish nothing.
    """
    with open(pyproject, "rb") as f:
        deps = tomllib.load(f).get("project", {}).get("dependencies", [])
    return _select(deps, RUNTIME_WANTED, f"{pyproject}'s [project].dependencies")


if __name__ == "__main__":
    args = sys.argv[1:]
    read = runtime_specs if "--runtime" in args else dev_specs
    specs = read(next(a for a in args if not a.startswith("--")))
    if "--canonical" in args:
        specs = [_canonical(s) for s in specs]
    print("\n".join(specs))
