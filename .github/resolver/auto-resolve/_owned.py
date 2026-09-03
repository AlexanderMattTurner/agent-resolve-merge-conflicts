"""Which paths the CALLING repository's own generators own, read in ONE place.

PROBLEM CLASS — decoding `resolve-generated.mjs --owned` in more than one place.
That command prints one generator-owned path per line, and a rule's `ownsPrefix`
as a directory with a TRAILING SLASH — the form that covers a set the per-file
enumeration cannot list, such as every vendored `dist-info` file a dependency
bump renames. Readers that decoded it independently disagreed about what they
covered, so one of them silently answered "not owned" for a whole owned tree.

Every reader fails CLOSED. An ownership answer that cannot be read would route a
caller-owned lockfile to this resolver's built-in rules, which is exactly the
precedence the caller's own table exists to hold.
"""

import os
import shlex
import subprocess
from dataclasses import dataclass

#: Names the rule table as an ABSOLUTE path inside the TRUSTED BASE checkout.
#: The tree under review must never be able to declare its own.
RESOLVER_ENV = "AUTO_RESOLVE_RESOLVER_MJS"


@dataclass(frozen=True, slots=True)
class Owned:
    """One ownership answer: exact paths, and the directories a rule claims whole."""

    exact: frozenset[str]
    prefixes: tuple[str, ...]

    def covers(self, path: str) -> bool:
        """Whether a regeneration rule owns PATH, by exact match or by directory.

        Exact equality alone misses a file under an owned subtree, which the
        resolver would then regenerate with ITS OWN rule instead of the caller's
        — the one thing the caller's table exists to prevent.
        """
        return path in self.exact or path.startswith(self.prefixes)


EMPTY = Owned(frozenset(), ())


def parse(text: str) -> Owned:
    """`--owned`'s output as the two forms it prints, one entry per line.

    Each line is stripped before it is classified. A padded line otherwise
    yields the entry `" vendor/ "`, which ends in a space rather than a slash,
    so it is filed as an exact path and stops covering its own subtree.
    """
    lines = [stripped for line in text.splitlines() if (stripped := line.strip())]
    return Owned(
        exact=frozenset(line for line in lines if not line.endswith("/")),
        prefixes=tuple(line for line in lines if line.endswith("/")),
    )


def load(resolver: str, *flags: str) -> Owned:
    """The rule table's answer, asked of RESOLVER under `node`.

    Under `node` and never under a package manager: `pnpm` parses package.json,
    which mid-merge can carry conflict markers, while `--owned` parses no
    manifest. A non-zero exit raises, because an empty answer from a broken
    table is indistinguishable from a correct empty one.
    """
    argv = ["node", resolver, "--owned", *flags]
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(
            f"{shlex.join(argv)} failed (exit {done.returncode}): "
            f"{done.stderr.strip() or '<no stderr>'}"
        )
    return parse(done.stdout)


def load_from_env(*flags: str) -> Owned:
    """The rule table `RESOLVER_ENV` names, or EMPTY when it names none.

    Unset is a caller that declares no generated files, and the empty answer is
    the true one there. Never a guessed default: a guessed path that misses
    prints nothing, which is the same output a correct empty answer gives.
    """
    resolver = os.environ.get(RESOLVER_ENV, "").strip()
    return load(resolver, *flags) if resolver else EMPTY
