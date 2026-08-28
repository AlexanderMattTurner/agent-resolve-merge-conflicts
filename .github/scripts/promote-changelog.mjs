// Promote the `## Unreleased` block in CHANGELOG.md to a dated version section.
//
// Invoked from `.github/scripts/version-bump.sh` after a successful
// `pnpm publish`. Reads the drafted release notes from environment variables so
// commit-message content is never interpolated into a shell heredoc:
//
//   NEW_VERSION        — the semver string, e.g. "1.2.3"
//   RELEASE_DATE       — "YYYY-MM-DD" in UTC
//   CHANGELOG_SECTION  — markdown body for the new dated section, used only
//                        when nothing else supplies one
//
// PROBLEM CLASS — a promotion that DROPS the notes it was asked to promote.
// The dated section takes every note a human wrote — the fragments under
// `changelog.d/`, then whatever is still under `## Unreleased` — and the
// drafted body is the fallback for a repository that curates neither.
// Reversing that precedence deletes hand-written notes on the first release,
// and the commit-subject list that replaces it reads enough like a changelog
// that the loss is invisible in review.
//
// WHY FRAGMENTS — every PR appending a bullet to one `## Unreleased` list makes
// that list a merge conflict between any two open PRs. A fragment is one file
// per PR, named `<id>.<category>.md`, so two PRs write two files and nothing
// conflicts. This script is what folds them back into one list at release.
//
// Behavior:
// - Writes diagnostics to stderr, successes to stdout.
// - Unexpected errors are logged and the process still exits 0. `pnpm publish`
//   has already succeeded by this point, and a CHANGELOG failure must not abort
//   the surrounding bash script (which still needs to create and push the git
//   tag).
// - File write is atomic (temp file + rename) so a crash mid-write leaves the
//   original CHANGELOG intact.
//
// Self-contained on purpose (node builtins only): the release workflow may run
// a trusted copy of this file, which only works if it imports nothing in-repo.

import {
  writeFileSync,
  readFileSync,
  readdirSync,
  renameSync,
  unlinkSync,
} from "node:fs";
import { dirname, basename, join } from "node:path";

const CHANGELOG_PATH = "CHANGELOG.md";
const FRAGMENT_DIR = "changelog.d";

// Keep a Changelog's categories, in the order a released section prints them.
const CATEGORIES = [
  "added",
  "changed",
  "deprecated",
  "removed",
  "fixed",
  "security",
];
const FRAGMENT_RE = new RegExp(`^(.+)\\.(${CATEGORIES.join("|")})\\.md$`);

/**
 * @param {string} message
 */
function warn(message) {
  process.stderr.write(`CHANGELOG update: ${message}\n`);
}

/**
 * Write `contents` to `path` atomically: write a sibling temp file, then rename
 * it over the target. A crash mid-write leaves the original file intact.
 * @param {string} path
 * @param {string} contents
 */
function atomicWrite(path, contents) {
  const tmp = join(dirname(path), `.${basename(path)}.${process.pid}.tmp`);
  writeFileSync(tmp, contents);
  renameSync(tmp, path);
}

/**
 * @returns {{newVersion: string, releaseDate: string, section: string} | null}
 */
function readEnv() {
  const required = ["NEW_VERSION", "RELEASE_DATE", "CHANGELOG_SECTION"];
  const values = Object.create(null);
  for (const name of required) {
    const value = process.env[name];
    if (!value) {
      warn(`missing required env var ${name}; skipping.`);
      return null;
    }
    values[name] = value;
  }
  return {
    newVersion: values.NEW_VERSION,
    releaseDate: values.RELEASE_DATE,
    section: values.CHANGELOG_SECTION,
  };
}

/**
 * Strip a leading "## [vX.Y.Z]" heading the model may have emitted despite
 * being told "body only", then trim trailing whitespace. Returns the empty
 * string if nothing substantive is left.
 * @param {string} raw
 * @returns {string}
 */
function normalizeBody(raw) {
  return raw.replace(/^\s*## \[[^\]]+\][^\n]*\n+/, "").trimEnd();
}

/**
 * The pending fragments under `changelog.d/`, as one categorised markdown body
 * plus the files that produced it.
 *
 * A name this cannot parse is REPORTED, never skipped silently: the whole point
 * of a fragment is that a user-facing note survives to the release, so a typo in
 * a category is a note about to be dropped.
 *
 * @returns {{body: string, consumed: string[]}}
 */
function readFragments() {
  /** @type {string[]} */
  let names;
  try {
    names = readdirSync(FRAGMENT_DIR);
  } catch {
    // No fragment directory is the ordinary state of a repository that has not
    // adopted them, so it is not worth a warning.
    return { body: "", consumed: [] };
  }

  /** @type {Map<string, {id: string, text: string}[]>} */
  const byCategory = new Map();
  const consumed = [];
  for (const name of names.sort()) {
    if (name === "README.md") continue;
    const match = name.match(FRAGMENT_RE);
    if (!match) {
      warn(
        `${join(FRAGMENT_DIR, name)} is not named <id>.<category>.md with a ` +
          `category among ${CATEGORIES.join(", ")}; it is NOT in this release.`,
      );
      continue;
    }
    const [, id, category] = match;
    const path = join(FRAGMENT_DIR, name);
    const text = readFileSync(path, "utf8").trimEnd();
    if (!text) {
      warn(`${path} is empty; it is NOT in this release.`);
      continue;
    }
    const entries = byCategory.get(category) ?? [];
    entries.push({ id, text });
    byCategory.set(category, entries);
    consumed.push(path);
  }

  const sections = [];
  for (const category of CATEGORIES) {
    const entries = byCategory.get(category);
    if (!entries) continue;
    // By id, numerically where both ids are numbers, so 9 sorts before 10.
    entries.sort((a, b) => {
      const left = Number(a.id);
      const right = Number(b.id);
      if (Number.isFinite(left) && Number.isFinite(right) && left !== right) {
        return left - right;
      }
      return a.id.localeCompare(b.id);
    });
    const heading = category[0].toUpperCase() + category.slice(1);
    sections.push(
      `### ${heading}\n\n${entries.map((entry) => entry.text).join("\n")}`,
    );
  }
  return { body: sections.join("\n\n"), consumed };
}

/**
 * Delete the fragments this release folded in. A failure here is reported and
 * survived: the notes already reached CHANGELOG.md, and leaving a stale file
 * behind costs a duplicate entry next release, which a human can see and fix.
 * @param {string[]} paths
 */
function dropFragments(paths) {
  for (const path of paths) {
    try {
      unlinkSync(path);
    } catch (err) {
      warn(
        `could not delete the consumed fragment ${path}: ` +
          `${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }
}

/**
 * Locate the `## Unreleased` block and return the text before it, the block's
 * own body, and the text after it (starting at the next `## ` heading). Returns
 * null if there is no Unreleased heading.
 * @param {string} source
 * @returns {{before: string, body: string, afterBlock: string} | null}
 */
function splitAroundUnreleased(source) {
  const markerMatch = source.match(/^## Unreleased[ \t]*$/m);
  if (!markerMatch || markerMatch.index === undefined) return null;

  const blockStart = markerMatch.index + markerMatch[0].length;
  const rest = source.slice(blockStart);
  const nextHeadingOffset = rest.search(/\n## /);
  const bodyEnd =
    nextHeadingOffset === -1 ? source.length : blockStart + nextHeadingOffset;

  return {
    before: source.slice(0, markerMatch.index),
    body: source.slice(blockStart, bodyEnd),
    afterBlock: source.slice(bodyEnd),
  };
}

function promoteUnreleased() {
  const env = readEnv();
  if (!env) return;

  const source = readFileSync(CHANGELOG_PATH, "utf8");
  const split = splitAroundUnreleased(source);
  if (!split) {
    warn(`no "## Unreleased" heading in ${CHANGELOG_PATH}; skipping.`);
    return;
  }

  // Every hand-written note, then the drafted body only when there is none:
  // the legacy Unreleased text first, because it carries no `###` heading and
  // would otherwise read as part of the last fragment category.
  const fragments = readFragments();
  const written = [normalizeBody(split.body), fragments.body]
    .filter(Boolean)
    .join("\n\n");
  const body = written || normalizeBody(env.section);
  if (!body) {
    warn("no fragment, no Unreleased text and no drafted body; skipping.");
    return;
  }

  const dated = `## [${env.newVersion}] - ${env.releaseDate}\n\n${body}\n`;
  const afterBlock = split.afterBlock.replace(/^\n+/, "\n");
  const updated = `${split.before}## Unreleased\n\n${dated}${afterBlock}`;

  atomicWrite(CHANGELOG_PATH, updated);
  // After the write, never before: a fragment deleted ahead of a failed write
  // is a note that reached no changelog at all.
  dropFragments(fragments.consumed);
  process.stdout.write(
    `Promoted Unreleased → [${env.newVersion}] - ${env.releaseDate} in ` +
      `${CHANGELOG_PATH} (${fragments.consumed.length} fragment(s))\n`,
  );
}

try {
  promoteUnreleased();
} catch (err) {
  // Exit 0 deliberately: pnpm publish has already succeeded at this point in
  // the release flow; a CHANGELOG hiccup must not abort the surrounding bash
  // script and skip the tag push.
  warn(
    `failed: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}`,
  );
}
