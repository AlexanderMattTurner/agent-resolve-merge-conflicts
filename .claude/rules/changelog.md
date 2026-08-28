---
paths:
  - "CHANGELOG.md"
  - "changelog.d/**"
  - ".github/scripts/promote-changelog.mjs"
---

# Changelog

**Never edit `CHANGELOG.md` by hand — `## Unreleased` is a static header.** Two branches appending to one list is a merge conflict between every pair of open pull requests, and the resolver then spends a paid run on it.

**Add a fragment instead: `changelog.d/<id>.<category>.md`**, where `<id>` is the pull request number and `<category>` is one of `added`, `changed`, `deprecated`, `removed`, `fixed`, `security`. The body is the bullet or bullets that go under that heading. Write it in the same commit as the change. [`changelog.d/README.md`](../../changelog.d/README.md) carries the rest.

**Write one for a change a consumer of this workflow could notice** — a new or changed input, a different default, a resolution that now succeeds where it failed, a security boundary that moved. Internal churn gets none: a test refactor, a comment, CI plumbing nobody outside this repository runs.

`.github/scripts/promote-changelog.mjs` folds every pending fragment into the new dated section at release and deletes the ones it consumed. It reports a misnamed or empty fragment and leaves the file alone, so a typo costs a rename rather than a lost note. **Never reword or delete a released entry** — a shipped line is an audit record.
