// `.github/scripts/promote-changelog.mjs` — the release-time fold of
// `changelog.d/` fragments into a dated CHANGELOG section.
//
// Driven as a subprocess in a scratch repository, because the script reads
// CHANGELOG.md and changelog.d/ relative to the working directory and deletes
// the fragments it consumed. A stub of the filesystem would test neither.

import { spawnSync } from "node:child_process";
import {
  mkdtempSync,
  mkdirSync,
  writeFileSync,
  readFileSync,
  readdirSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import assert from "node:assert/strict";

const SCRIPT = resolve(".github/scripts/promote-changelog.mjs");

const HEADER = "# Changelog\n\nPreamble.\n\n";
const PRIOR = "\n## [1.0.0] - 2026-01-01\n\n- the release before this one\n";

/** A scratch repository with a CHANGELOG and the named fragments. */
function fixture(fragments, unreleasedBody = "\n") {
  const dir = mkdtempSync(join(tmpdir(), "promote-changelog-"));
  writeFileSync(
    join(dir, "CHANGELOG.md"),
    `${HEADER}## Unreleased\n${unreleasedBody}${PRIOR}`,
  );
  if (fragments) {
    mkdirSync(join(dir, "changelog.d"));
    for (const [name, body] of Object.entries(fragments)) {
      writeFileSync(join(dir, "changelog.d", name), body);
    }
  }
  return dir;
}

function promote(dir, overrides = {}) {
  const env = {
    ...process.env,
    NEW_VERSION: "1.1.0",
    RELEASE_DATE: "2026-02-02",
    CHANGELOG_SECTION: "- drafted from commits",
    ...overrides,
  };
  const done = spawnSync(process.execPath, [SCRIPT], {
    cwd: dir,
    encoding: "utf8",
    env,
  });
  assert.equal(done.status, 0, done.stderr);
  return {
    stdout: done.stdout,
    stderr: done.stderr,
    changelog: readFileSync(join(dir, "CHANGELOG.md"), "utf8"),
  };
}

test("fragments become one categorised section, oldest id first", () => {
  const dir = fixture({
    "10.fixed.md": "- fixed the tenth thing\n",
    "9.fixed.md": "- fixed the ninth thing\n",
    "7.added.md": "- added a thing\n",
    "README.md": "not a fragment\n",
  });
  const { changelog } = promote(dir);
  const section = changelog.slice(changelog.indexOf("## [1.1.0]"));
  assert.match(section, /## \[1\.1\.0\] - 2026-02-02/);
  // Added before Fixed, and 9 before 10 — a lexical sort would invert both.
  assert.match(
    section,
    /### Added\n\n- added a thing\n\n### Fixed\n\n- fixed the ninth thing\n- fixed the tenth thing\n/,
  );
  // The README is not a note, and the drafted fallback never ran.
  assert.doesNotMatch(section, /not a fragment|drafted from commits/);
});

test("consumed fragments are deleted and the README is kept", () => {
  const dir = fixture({
    "7.added.md": "- added a thing\n",
    "README.md": "keep me\n",
  });
  promote(dir);
  assert.deepEqual(readdirSync(join(dir, "changelog.d")), ["README.md"]);
});

test("a misnamed fragment is reported, not silently dropped", () => {
  const dir = fixture({
    "7.improved.md": "- a note under a category that is not one\n",
  });
  const { stderr, changelog } = promote(dir);
  assert.match(stderr, /7\.improved\.md is not named <id>\.<category>\.md/);
  assert.match(stderr, /NOT in this release/);
  // The note reaches no release, and its file survives for a human to rename.
  assert.doesNotMatch(changelog, /a note under a category that is not one/);
  assert.ok(readdirSync(join(dir, "changelog.d")).includes("7.improved.md"));
});

test("an empty fragment is reported rather than releasing a blank bullet", () => {
  const dir = fixture({ "7.fixed.md": "\n" });
  const { stderr, changelog } = promote(dir);
  assert.match(stderr, /7\.fixed\.md is empty/);
  assert.doesNotMatch(changelog, /### Fixed/);
});

test("hand-written Unreleased text is kept, above the fragment sections", () => {
  const dir = fixture(
    { "7.fixed.md": "- from a fragment\n" },
    "\n- written by hand under Unreleased\n",
  );
  const { changelog } = promote(dir);
  const section = changelog.slice(changelog.indexOf("## [1.1.0]"));
  assert.match(
    section,
    /- written by hand under Unreleased\n\n### Fixed\n\n- from a fragment/,
  );
});

test("no fragments and no hand-written text falls back to the drafted body", () => {
  const dir = fixture(null);
  const { changelog } = promote(dir);
  assert.match(
    changelog,
    /## \[1\.1\.0\] - 2026-02-02\n\n- drafted from commits/,
  );
});

test("a release whose notes are all fragments needs no drafted body", () => {
  // The drafted body is the FALLBACK. Requiring it skipped the promotion here,
  // which shipped the release and left the fragments for the next one to claim.
  const dir = fixture({ "7.fixed.md": "- fixed a thing\n" });
  const { changelog } = promote(dir, { CHANGELOG_SECTION: "" });
  assert.match(
    changelog,
    /## \[1\.1\.0\] - 2026-02-02\n\n### Fixed\n\n- fixed a thing/,
  );
  assert.deepEqual(readdirSync(join(dir, "changelog.d")), []);
});

test("a missing version is still a skip, and consumes no fragment", () => {
  const dir = fixture({ "7.fixed.md": "- fixed a thing\n" });
  const { stderr, changelog } = promote(dir, { NEW_VERSION: "" });
  assert.match(stderr, /missing required env var NEW_VERSION/);
  assert.doesNotMatch(changelog, /1\.1\.0/);
  assert.deepEqual(readdirSync(join(dir, "changelog.d")), ["7.fixed.md"]);
});
