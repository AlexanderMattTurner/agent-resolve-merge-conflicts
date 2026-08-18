# Promote the local check pack into ci-truth-serum

You are moving pre-commit checks out of two consumer repositories and into the
package that publishes them, so each check has ONE definition instead of a copy
per repo. Work in `ci-truth-serum` first; the consumer changes come last.

## The problem

`agent-resolve-merge-conflicts` and `claude-automation-template` each carry the
same 25 checks under `.github/scripts/checks/`, ported file by file. A copy per
repo is the duplication `check-lockstep-pins` exists to expose: a fix to one copy
does not reach the other, the two drift, and a third repo copies whichever it
found first. `ci-truth-serum` already publishes checks as pre-commit hook ids
that a consumer pins by `rev`, which is the shape these want.

## What moves and what stays

A check belongs upstream when its rule is true of any repository that runs CI.
It stays local only when it encodes a policy this repo chose, and the policy —
never the mechanism — is what cannot move.

**Move as-is.** `bare-mkdir`, `curl-retry`, `retry-loop`, `unbounded-waits`,
`env-arith`, `shell-source-declarations`, `dead-shell-functions`,
`cwd-scoped-git`, `unspecified-encoding`, `duplicate-module-constant`,
`duplicate-class-names`, `big-tuple-annotations`, `unreset-module-state`,
`sleep-as-sync`, `wall-clock-assertions`, `positional-git-argv`,
`test-helper-kwargs`, `truncating-pr-json`, `path-shadowed-interpreter`,
`sparse-checkout-closure`, `relative-imports`.

**Move the engine, keep the policy.** `file-size`, `comment-block-length` and
`unspecified-encoding` are grandfathered ratchets. The ratchet mechanism —
baseline, cap, stale-entry detection — is generic and moves. The cap and the
baseline JSON are this repo's answer and stay in `config/`, passed by flag.

**Move with a flag, because each names a repo path today.** `retry-loop` cites
`.github/scripts/lib-ci-retry.sh` as the one retry helper; `curl-retry` scans "this
repo's shell"; `gate-hooks-shimmed` requires `safe-launch.sh`; `grant-wildcards`
reads the matching semantics of `.claude/hooks/lib-check-bash.sh`. Each becomes a
CLI option with the current value as the consumer's argument, not a constant in
the check.

**Decide, do not assume.** For anything not listed, apply the test above and say
which side it fell on and why. A check nobody outside this family would want is
a check that stays local — say so rather than moving it for symmetry.

## How to do it

1. **Read the upstream conventions first.** `ci-truth-serum`'s `CLAUDE.md`,
   `.pre-commit-hooks.yaml`, `ci_truth_serum/run_tier.py` (the `TIERS` registry),
   and two or three existing checks. Match them: one module per check under
   `ci_truth_serum/`, a docstring stating the failure it prevents, an opt-out
   slug built through `_linecheck.annotation_re`, placement decided by
   `_linecheck.annotation_window`, and no hand-rolled `token in line` predicate.
2. **Reuse the upstream helpers rather than the ported ones.** `_bash_ast.py`,
   `_linecheck.py`, `_py_ast.py`, `_js_ast.py` and `_comments.py` already exist
   there. A ported check carrying its own copy of a helper must be rewritten onto
   the upstream one — that is the whole point of the move.
3. **Assign each check a tier.** Tier 1 is honesty and identity, Tier 2 is
   opinionated, Extras is off-theme. A check that only bites in a repo with this
   family's layout is Extras at most. Put it in the `TIERS` registry so a
   consumer that enables the aggregate gets it with no config change.
4. **Port each check's tests too**, adapted to upstream's test layout. A check
   that arrives without its tests is a check nobody can change safely.
5. **Cut a release.** Follow upstream's own release process — read its
   `CHANGELOG.md` and `changelog.d/`. Tag it, because a consumer pinning an
   untagged `main` SHA has no readable version, and this family's last bump had
   to accept exactly that.
6. **Then the consumers, one PR each.** In each repo: bump the `rev` to the new
   tag in `.pre-commit-config.yaml` AND in `.github/workflows/sync-required-checks.yaml`
   (`check-lockstep-pins` enforces that pair), enable the new hook ids, pass the
   flags each parameterized check needs, and DELETE the local script, its tests
   and its `- repo: local` hook entry in the same commit. A local copy left
   beside the published one is the defect this work removes.

## Constraints

- **Deleting the local copy is not optional and not a follow-up.** A consumer PR
  that adds the upstream hook without removing the local script has doubled the
  duplication rather than ended it.
- **Behaviour must not change in the move.** Run the moved check against the
  consumer tree before and after and show the finding sets are identical. When a
  finding set changes, that is a bug in the port until you can name why the new
  answer is the right one.
- **A check that imports `tree_sitter`, `tree_sitter_bash` or `yaml` needs those
  in its hook's `additional_dependencies`**, pinned. Under `language: system` it
  passes on a developer's virtualenv and dies in pre-commit's own environment.
- **Never disable, skip or work around a hook** to get a green run, and never use
  `--no-verify`.
- Do not merge any PR. Open it and report.

## Report

For each check: where it landed (upstream tier, or local with the reason), the
flags it gained, and the before/after finding counts in both consumer repos. Then
the three PR links, and the exact commands you ran with their exit codes.
