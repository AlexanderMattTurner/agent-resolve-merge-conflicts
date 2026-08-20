# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to
adhere to [Semantic Versioning](https://semver.org/).

Add user-facing changes under `## Unreleased` as you make them. This repository
publishes no package, so a release is a git tag: on each push to the default
branch, `release-tags.yaml` tags `vX.Y.Z`, advances the moving major tag (`v1`)
to the same commit, and promotes the `## Unreleased` block into a new dated
`## [version]` section below it (see `.github/scripts/release-tag.sh`).

## Unreleased

## [1.5.1] - 2026-08-20

- fix(release): drop the apostrophes shellcheck reads as a quote
- fix(release): advance only this repository's own resolver pin
- fix(auto-resolve): declare the inputs the resolver dispatches, and move the pin with the tag
- fix(auto-resolve): stop the carry cap from binding before the set shrinks
- ci(auto-resolve): pin the caller at v1.4.0, where the carry lives

## [1.5.0] - 2026-08-20

- fix(auto-resolve): only call a decline a revert when the base side changed
- style(auto-resolve): trim the comment back under the 5-line cap
- style(auto-resolve): describe current test behavior, not its history
- fix(auto-resolve): close correctness gaps the review found in the four fixes
- refactor(auto-resolve): move the mechanical-merge derivation beside its text logic
- chore(auto-resolve): mark the seam helper executable
- fix(auto-resolve): keep a failing lock command's output out of the verdict stream
- test(auto-resolve): reach the seam helper through load_script
- feat(auto-resolve): route a recognized lockfile to its own lock command
- feat(auto-resolve): report a seam when a decline drops a name callers still use
- feat(auto-resolve): refuse a resolution that rewrites lines outside a conflict
- fix(auto-resolve): verify a regenerated lockfile instead of reviewing it
- fix(auto-resolve): refuse a decline whose kept side reverts the base

## [1.4.0] - 2026-08-20

- ci(auto-resolve): advance the caller pin past the optional hook pins
- fix(auto-resolve): mark the two new resolver scripts executable
- refactor(auto-resolve): reach git through the bound runner in the carry step
- feat(auto-resolve): carry a partial resolution into the next round
- fix(auto-resolve): hand over a prompt only where a shard recorded a decline
- fix(auto-resolve): keep MAX_PARALLEL a cap, and hand a decision over as a prompt
- fix(auto-resolve): cover the conflict set, and name the clock that cut it

## [1.3.1] - 2026-08-20

- fix(review): let a push deliver the note a skipped PR never got
- fix(review): stop the needs-auto-review label burning the ladder on a bot PR
- fix(ci): validate a millisecond retry knob instead of dividing it inline
- refactor(ci): one retry ladder, not three
- fix(resolver): keep the evidence request in a decline's closing sentence
- fix(ci): sparse-checkout the library auto-approve-skipped-pr.sh sources
- fix(resolver): stop a DECLINE telling the reader to file a resolver bug

## [1.3.0] - 2026-08-20

- refactor(auto-resolve): read the fork sentinel through untrusted_head()
- fix(auto-resolve): stop a fork head's resolution dying on a missing pnpm
- fix(checks): derive the repo root from the ratchet SSOT in test-helper-kwargs
- docs(review-gate): correct why the gate posts a status rather than a check run
- style(review-gate): keep the term-(c) note inside the comment-block cap
- fix(review-gate): stop a label-event skip standing in for a merge-delta verdict
- feat(review): gate merges on a review-findings status, not a reviewer vote

## [1.2.1] - 2026-08-20

- fix(ci): pin the run context the golden corpus records, and drop a source-text test
- style(resolver): split three comment blocks under the 5-line cap
- fix(resolver): release a concluded run's mark before taking its head on
- style(resolver): bring two comment blocks under the 5-line cap
- style(resolver): apply shfmt and mark the two new entry points executable
- fix(resolver): stop a run that resolves nothing reporting success

## [1.2.0] - 2026-08-19

- test(resolver): point the script harness at a base repo it can fetch
- test(resolver): give the race suite a base repository land can fetch
- docs(resolver): shorten the optional-pin note to its cap
- fix(resolver): install a hook binary only when the caller pins one
- fix(resolver): read the untrusted-head flag in the hook, not through fanout
- fix(resolver): confine a fork run's reads, and read its base from the base repo
- fix(resolver): converge the fork-head Node pin on this repo's SHA
- feat(resolver): resolve a fork head that allows maintainer edits

## [1.1.3] - 2026-08-19

- refactor(resolver): move the fan-out's reporting surface to its own module
- test(resolver): regenerate the bundle golden for the two no-record refusals
- refactor(resolver): name the ladder walk's three results
- fix(resolver): carry a shard's decline through every consumer of it
- fix(resolver): keep the shard-decline fix inside both size ratchets
- fix(resolver): give a shard one channel to say it declined

## [1.1.2] - 2026-08-19

- fix(resolver): stop the clone-failed comment asserting an outcome it cannot read
- fix(resolver): count both verdict kinds against one cap, and page the marks
- refactor(resolver): move the resolver-change reads out of discover.py
- fix(resolver): retire a paid verdict once the base moves under it
- fix(resolver): comment on the pull request when a run ends badly

## [1.1.1] - 2026-08-19

- test(ci): split the blind-ladder test that raced its own budget
- fix(ci): keep a derived file out of the supersession filter too
- fix(ci): read the -merge rule at the merge tree, and escape the path
- fix(ci): read the -merge rule at the PR head as well as the checkout
- test(ci): pass encoding to the new merge-delta test writes
- fix(ci): stop per-hunk tracing from clearing a derived file's resolution

## [1.1.0] - 2026-08-19

- test(resolver): pin that a refusal never speaks over a run that did work
- refactor(resolver): read the three spend knobs as repository variables
- fix(resolver): name every rail holding a PR, and keep a draft fork silent
- feat(resolver): make every auto-resolve refusal visible and its knobs reachable
- docs(github): name the secret route (a) now depends on
- docs(ci): state that the failure notifier needs GH_NTFY_SUBJECT
- feat(ci): notify ntfy instead of filing ci-failure issues

## [1.0.0] - 2026-08-19

### Changed

- The release flow is live. `release-tags.yaml` passes `RELEASE_DRY_RUN=false`
  unless the repository variable holds a non-empty override, so a push to the
  default branch carrying a releasable commit cuts `vX.Y.Z` and advances `v1`. A
  caller can now pin a version instead of a bare SHA. Thirteen dry-run cycles had
  reported the version they would cut and written nothing, which left the whole
  live half — the changelog commit, the atomic push, the major-tag move and its
  repair — unexecuted anywhere; `tests/test_release_tag.py` drives it against a
  real remote.

### Fixed

- `promote-changelog.mjs` promotes the curated `## Unreleased` notes into the
  dated section instead of replacing them with the commit log. It used to write
  `CHANGELOG_SECTION` — for the tag flow, up to 40 raw commit subjects — over the
  block, so the first release would have deleted every hand-written note here,
  and a subject list reads enough like a changelog for the loss to pass review.
  The drafted body is now the fallback for a repository that curates nothing.

### Added

- Every auto-resolve refusal is now visible. `discover.py` words each one once and
  routes it to the run log, `$GITHUB_STEP_SUMMARY`, and — for a scan scoped to one
  PR — `refused_rail`/`refused_reason` outputs, which a new `resolve`-job step
  publishes as the PR's sticky status comment (`STATE=refused`). A conflicted PR
  whose head is in a FORK also gets a one-time notice: that refusal never lifts,
  because the resolver's token is read-only there, and it was the only rail with
  neither a log line nor a comment.
- Three spend knobs the reusable workflow could not reach: `max-commit-age-hours`,
  `attempt-ttl-hours` and `attempt-floor-hours`. The age input's empty default used
  to overwrite `AUTO_RESOLVE_MAX_COMMIT_AGE_HOURS` on every non-catch-up run, so a
  caller could never widen the 24h window, and the TTL and floor never reached the
  resolve job's discover step at all. `auto-resolve-conflicts.yaml`'s own scan job
  also reads `vars.AUTO_RESOLVE_ATTEMPT_FLOOR_HOURS` now, beside the TTL it already
  read.

- `Automated review posted`, a **required** check that makes auto-merge wait for
  the automated reviewer. The cheap checks finish in about ninety seconds while
  an LLM review takes minutes, so a PR gated only on the cheap checks merged
  first and the reviewer's `REQUEST_CHANGES` landed on a PR that had already
  merged. `review-gate.yaml` posts the verdict as a commit status on each PR
  head: green once an undismissed review of the PR exists, `pending` before that,
  and `pending` again if the last review is dismissed. The context name is
  registered by a never-firing job in `review-gate-context.yaml`, because a job
  sharing the name would report its own green check run under the same context
  and satisfy the gate while the status still said `pending`.

- PreToolUse skill gates: opening a PR, writing a test file, or writing a plan is
  denied until the session has invoked `pr-creation`, `writing-tests`, or
  `explore-plan`. The rules already said to invoke them; the gates are what makes
  a session that skimmed past the rule notice. Each covers the CLI route and the
  MCP route, since locking one door only moves the session to the other, and each
  fails OPEN — an unusable session id or an unparsable payload costs the reminder,
  never the tool call.

- `shell-targets`, a decide-gate input that DERIVES a gate's watched paths from
  the shell entry point the job runs, instead of restating them in `paths-regex`
  where the copy drifts silently. `.github/scripts/shell-run-closure.py` walks
  every in-repo file the entry point can reach, following the paths it EXECUTES
  as well as those it sources, and reaching a target written as
  `"$root/path/to/x.sh"` through the token's path suffix. It over-approximates
  on purpose, so it combines with `paths-regex` rather than replacing it.
- Three skills ported from the downstream `agent-glovebox` tree: `git-workflow`
  (commit/push mechanics, who owns a merge conflict, auditing a bot's merge
  delta), `babysit-prs` (watch sets, mergeability and merge-queue state,
  re-arming auto-merge, which wake-ups deserve a reply), and `defect-to-guard`
  (turning a defect class into a guard PROPOSAL, and the arithmetic it must
  show). `CLAUDE.md` now points at them instead of carrying their rules inline.
- `.claude/rules/code-style.md`, which loads with any source file and carries the
  cross-language rules that used to sit in `CLAUDE.md` — plus asking the tool
  that owns a format, deleting a reimplementation once its replacement lands,
  "a change that makes a defect rarer is not a fix", the comment-block cap, and
  the no-drift-guard rule.
- A `Writing` section in `CLAUDE.md` governing every word a session produces, and
  an `End-of-session handoff` section covering what a session could not fix.
- The `decide` reusable workflow diffs the change range itself instead of calling
  `dorny/paths-filter`, and gains the inputs that go with it: `paths-regex`,
  `paths-regex-file` (an SSOT a local git hook can source too), `pytest-targets`
  (watched paths derived from a test's own import lines), `trigger-keyword`,
  `keyword-scope`, `skip-on-draft`, `ignore-comment-only-changes`,
  `shell-targets`, and `memoize-anchor-jobs`. It now gates `push`
  and `merge_group` events on their own ranges, re-anchors a stale webhook base
  to the live base tip so a merge commit stops over-triggering every gate, and
  fails loud on a gate configured with no trigger at all.
- A memo shadow on the decide job: `decide-memo-base.py` names the newest commit
  on the branch whose work job actually PASSED, and the gate logs what it would
  decide diffing from there. Logged only — nothing acts on it yet.

### Fixed

- Template-sync no longer introduces `auto-version.yaml` into a repo that does
  not already have it (new `OPT_IN_PATHS` mechanism). A consumer with its own
  release workflow used to end up with two publishers on the default branch;
  their concurrency groups differ, so both computed the same semver bump and the
  loser died on an `npm error code E404 … PUT` that named no duplicate. Adopting
  the workflow is copying the file in once; opting out is deleting it.
- `version-bump.sh` recognizes losing that race instead of failing on it: it
  skips when the version's tag is already on the remote, and classifies a
  publish `E404` by re-probing the registry rather than by reading the message.
  A 404 on a version that is genuinely absent still fails loud.
- The release checkout accepts an optional `RELEASE_BYPASS_TOKEN` (an own-owner
  PAT registered as a ruleset bypass actor) and falls back to `GITHUB_TOKEN`, so
  a protected default branch no longer rejects the release commit and tag with
  GH013 and strands every release.

### Changed

- The template's own JavaScript is linted (`eslint.config.mjs`, wired into
  pre-commit) under the rule set a consumer running `eslint .` over its whole
  tree would apply. Template-owned files previously contributed dozens of errors
  to consumers' lint, blocking publishing where a release gates on it.

### Added

- `check-pipefail-sigpipe.py` pre-commit lint: under `set -o pipefail`, a pipe
  consumer that stops reading mid-stream (`head -N`, `grep -q/-l/-m`, `sed '5q'`)
  SIGPIPEs its still-writing producer, so the pipeline exits 141 and `set -e`
  aborts — on exactly the large inputs the cap exists for, and only on a slow
  enough machine to be invisible in local testing. Detection is a real bash AST
  (`tree-sitter-bash`), fires only in scripts that enable `pipefail`, and a
  provably-bounded producer opts out with `# sigpipe-ok: <reason>`.
- `drop-superseded-ci-events.mjs` UserPromptSubmit hook: when a subscribed PR
  delivers a red CI-failure webhook whose HeadSHA no longer heads any remote
  branch (a newer push already superseded that run), the turn is ended before
  the model runs instead of burning a full-context turn to conclude "ignore it".
  Fails open on any uncertainty (control-plane package unavailable during a cold
  start, unparsable payload, git unavailable, or the SHA still being a live head).
- Hooks now cross the agent boundary through the `agent-control-plane-core`
  package (added as a runtime dependency, provisioned by `session-setup.sh`'s
  existing `pnpm install`) via the new `.claude/hooks/lib-control-plane.mjs` and
  `lib-hook-io.mjs` helpers, so the Claude hook wire-format has one source of
  truth instead of being hand-rolled per hook.
