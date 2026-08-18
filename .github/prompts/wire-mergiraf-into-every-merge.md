# Wire mergiraf into every merge, not just the resolver's

You are closing the gap between what `.gitattributes` promises and where
mergiraf actually runs. Work in `agent-resolve-merge-conflicts` first, then port
the same change to `claude-automation-template`.

## The problem

`.gitattributes` marks 13 file types `merge=mergiraf`, so git should merge them
by syntax node instead of by line. Those attributes are inert until two things
hold in the checkout doing the merge: the binary is on `PATH`, and
`merge.mergiraf.driver` is set in git config. Git reports nothing when they do
not hold — it silently falls back to the built-in text driver.

Only two places establish both, and both are CI:

- `.github/workflows/auto-resolve.yaml` (the resolve job) runs
  `.github/resolver/install-mergiraf.sh`.
- `.github/workflows/template-sync.yaml` runs `.github/scripts/install-mergiraf.sh`.

Every other merge gets git's line merge. That includes the case this repository
exists for: an agent session that resolves a PR's conflict by hand, because the
resolver did not act or the session judged that it would not. Verify the gap
before you change anything — in a fresh session checkout, `command -v mergiraf`
finds nothing and `git config --get merge.mergiraf.driver` exits 1.

## The work

**1. Install and register in the session checkout.** `.claude/hooks/session-setup.sh`
provisions every other tool a session needs (`shfmt`, `gh`, `jq`, `ruff`,
`zizmor`) and never mentions mergiraf. Add it there, into `$HOME/.local/bin`,
which the hook already puts on `PATH` and exports through `CLAUDE_ENV_FILE`.

Call `.github/scripts/install-mergiraf.sh` rather than writing a second copy of
the driver string: that script owns the pinned download, the sha256 refusal, the
`solve -p` contract probe, and the `git config` pair. Skip the call when the
binary is present AND the driver is already registered, so a re-run costs
nothing. Failure is a `warn`, not a hard exit — this hook warns for every other
optional tool, and a session with no mergiraf must still start.

This edits `.claude/hooks/`, the highest-risk diff class, so it ships as its own
PR with nothing else in it.

**2. Decide about `setup.sh`.** That is the installer a downstream repository
runs to adopt the template. It configures git hooks and never registers the
merge driver, so every consumer inherits the same inert `.gitattributes`. Either
wire it up there too or say in one line why a consumer's own checkout is out of
scope.

**3. Collapse the two installers onto one definition.** `.github/scripts/install-mergiraf.sh`
(108 lines) and `.github/resolver/install-mergiraf.sh` (115 lines) are the same
script with divergent fixes, which is how a copy pair fails: each carries
something the other lacks.

- The resolver copy caches the tarball under `MERGIRAF_CACHE_DIR` so Codeberg is
  contacted only after a version bump, and its refusal names
  `pinned_tools.py refresh`.
- The scripts copy checks that `tool-versions.sh` exists before sourcing it,
  because under `set -euo pipefail` a missing file aborts before the refusal
  that would have named the cause.

Both register the driver and both verify the digest, so neither is wrong — they
are just each missing the other's improvement. Find out first whether
`.github/resolver/` is a self-contained bundle copied into other repositories.
If it is, the two copies are justified and the fix is to sync the missing halves
in both directions, with a line in each header saying why the copy exists. If it
is not, delete one and call the other.

**4. Check the language list.** `.gitattributes` says its list is
`mergiraf languages --gitattributes` filtered to the types this tree contains.
Re-derive both sides — the tracked extensions and mergiraf's supported set — and
report the difference rather than assuming the list is still right. The tracked
set is `py sh mjs yaml md bash json js yml toml`, plus `cfg`, `tla`, `txt`,
`jsonc`, `lock` and several dotfiles; confirm which of the latter mergiraf
actually parses instead of inferring it from the extension.

## How to prove it works

A post-condition, not an exit status. After the setup hook runs, in the session
checkout:

- `git config --get merge.mergiraf.driver` prints the driver command.
- A real merge exercises it: make two branches that each add a different key to
  the same JSON object or a different function to the same Python file, merge
  them, and assert the merge succeeds with no conflict markers. Run the same
  merge with `-c merge.mergiraf.driver=` cleared and show it conflicting. A test
  that passes both ways proves nothing.

Put that merge in the existing test suite, beside the other resolver tests, so
the wiring cannot rot back to inert without a red.

## Constraints

- Never bypass a hook, never `--no-verify`, never rewrite pushed history.
- The pinned version and digest live in `.github/tool-versions.sh`. Bump them
  together or not at all; an install that cannot verify must fail closed.
- Do not add a new required check or lint for this. Propose one under
  `## Proposed guards` in the PR body and stop there.
