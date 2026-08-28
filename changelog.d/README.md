# Changelog fragments

One file per pull request, so two pull requests never edit the same line.

`CHANGELOG.md`'s `## Unreleased` section is a static header. Editing it is what
makes every open pull request conflict with every other one: git sees two
branches appending to one list and cannot pick an order. A fragment is a file
named after your pull request, so there is nothing to conflict over.

## Writing one

Name the file `<id>.<category>.md`, where `<id>` is the pull request number and
`<category>` is one of `added`, `changed`, `deprecated`, `removed`, `fixed` or
`security`. The body is the markdown bullet or bullets that belong under that
heading.

```console
$ cat changelog.d/88.fixed.md
- The resolver reuses a prior run's partial resolution instead of re-buying it.
```

At release, `.github/scripts/promote-changelog.mjs` groups every pending
fragment under its `### Category` heading, folds them into the new dated
section, and deletes the files it consumed.

Write the fragment in the same commit as the change, with the real pull request
number where you have one. Nothing renames a mismatched `<id>`: it only has to
avoid colliding with another open pull request's fragment.

## When to write one

Write one for a change someone using this workflow could notice — a new or
changed input, a different default, a resolution that now succeeds where it
failed, a security boundary that moved. Internal churn — a test refactor, a
comment, CI plumbing nobody outside this repository runs — gets none.

**A release that has fragments prints those and nothing else.** Every dated
section before this mechanism was a list of commit subjects, because the drafted
body filled an empty `## Unreleased`. That body is now the fallback for a release
with no fragment at all, so from the first release carrying one, a change without
a fragment appears in the changelog nowhere — not even as its commit subject.
That is the point: a curated list beats a transcript. It is also why the judgement
above matters more than it used to.

A misnamed or empty fragment is reported at release time and left on disk,
never folded in silently. It reaches no release until someone renames it.
