# agent-resolve-merge-conflicts

A reusable GitHub Actions workflow that resolves one pull request's merge conflicts with its base branch, then pushes the result back as a normal merge commit.

It runs two passes. A deterministic pre-pass re-derives every conflicted DERIVED file — a generated artifact through its generator, a lockfile through its own lock command. An LLM pass then handles the SOURCE conflicts that remain. A conflict with neither a deterministic nor a textual resolution (a binary file, or a `-merge` file no rule owns) fails the run with a pull request comment BEFORE any model cost.

Callers pin this workflow by commit SHA. Nothing is published to a package registry, and the workflow is not listed on the GitHub Marketplace — see [Versioning](#versioning) and [Decisions](#decisions).

## Call it

```yaml
jobs:
  resolve:
    uses: AlexanderMattTurner/agent-resolve-merge-conflicts/.github/workflows/auto-resolve.yaml@<sha>
    with:
      pr: ${{ matrix.pr.number }}
      # Pass the SAME sha the `uses:` line names, so the workflow and the
      # scripts it runs come from one commit.
      resolver-repository: AlexanderMattTurner/agent-resolve-merge-conflicts
      resolver-ref: <sha>
    secrets:
      FAR_ANTHROPIC_API_KEY: ${{ secrets.FAR_ANTHROPIC_API_KEY }}
      # ... the remaining 9; see `.github/workflows/auto-resolve-conflicts.yaml`
      # in this repository for a block a consumer can copy verbatim.
```

`.github/workflows/auto-resolve-conflicts.yaml` here is both this repository's own caller and the reference one. It owns the `discover` job (which pull requests to hand over) and the `relay` job; this workflow owns everything else.

Every input fails closed when empty. No `log-redactor` publishes no fan-out logs. No `pre-pass-command` refuses to bundle a deferred generated file rather than shipping bytes no build produces. An empty `bot-actors` admits no bot.

## The trust model

Two jobs, and the split IS the security boundary.

`resolve` checks out the PULL REQUEST'S OWN HEAD to merge into it, and runs the model there. It holds `contents: read`, the Claude billing tokens, and `pull-requests: write` on the calling repository's own `GITHUB_TOKEN` — nothing else.

`land` holds every write credential (`AUTOFIX_TOKEN_ORG`, and the workflow-scoped `TEMPLATE_SYNC_TOKEN_ORG`) and runs none of the untrusted code.

The separation is load-bearing rather than tidy. Anything running in `resolve` can append to `$GITHUB_ENV` or `$GITHUB_PATH`, so the unit of exposure is the JOB, not the step. **A composite action runs in one job and cannot express this split.** One artifact crosses the boundary — a git bundle of the merge commit — and `land` treats it as untrusted: it requires both parents to be commits already on the two named branches, refuses conflict markers, and replays the merge in a tree the model never touched.

### The three trees

| Tree                      | Holds                              | Trusted |
| ------------------------- | ---------------------------------- | ------- |
| `${RUNNER_TEMP}/resolver` | this repository, at `resolver-ref` | yes     |
| `${RUNNER_TEMP}/base`     | the caller at `github.sha`         | yes     |
| `${GITHUB_WORKSPACE}`     | the pull request head, mid-merge   | **no**  |

Both trusted trees are a plain `git clone`, because `actions/checkout`'s `path` must sit under `$GITHUB_WORKSPACE`.

`resolver-mjs` arrives repo-relative and is absolutized against the trusted base checkout once, in the `base` step. One of its two readers is `remerge-diff-report.py`, which decides which deltas the self-review stops reading — so a relative path would let a pull request declare its own evil merge generator-owned.

## Secrets

The `secrets:` block names 10 secrets and never uses `secrets: inherit`. The list IS the contract a consumer configures against; inheriting would hand a resolver run every unrelated secret the calling repository holds. Each is optional — an unset ladder rung is dropped, and an unset org PAT narrows what `land` can push rather than failing the call.

`tests/test_auto_resolve_reusable_secrets.py` asserts the read set and the declared set are equal in BOTH directions, because an undeclared secret arrives empty and reads as a dead credential rather than as a mistake.

## Versioning

Each release tags `vX.Y.Z` and advances a moving major tag (`v1`) to the same commit.

**Callers still pin by SHA.** The major tag is not a supply chain you should depend on — it is what makes a bump reviewable: `v1` moving tells you a compatible release exists, and you update your pinned SHA deliberately.

## Tests

```bash
node --test .github/resolver/auto-resolve/*.test.mjs   # the resolver's own suite
uv run --extra dev pytest tests/ -q                    # the repository's suite
uvx pre-commit==4.6.0 run --all-files
uvx zizmor==1.25.2 .github/
```

## Decisions

**No GitHub Marketplace listing, and no root `action.yml`.** Listing requires an `action.yml` at the repository root, and only actions are listed — a reusable workflow cannot be listed at all. A thin root composite would be listable, but a composite runs in ONE job, so it cannot give the two-job privilege split that is the reason this workflow exists; publishing one would advertise a shape that silently drops the security boundary. A root `action.yml` also means one listing per repository, so it would cost any later automation here its own listing. SHA pinning needs no listing.
