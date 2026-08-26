#!/usr/bin/env node
// Drop GENERATED files' sections from a unified diff, so the automated reviewer
// reads the hand-written change instead of a rebuilt artifact.
//
// PROBLEM CLASS — a review budget spent on bytes nobody wrote. A repository that
// COMMITS a build output sends a diff whose size is set by the artifact, not by
// the edit: a 25-line source change can arrive as tens of thousands of lines,
// over the reviewer's diff-line budget, so the review is skipped and
// the source change gets no read at all. The artifacts need none: a required
// check rebuilds each and fails on any difference.
//
// The omit list comes from `resolve-generated.mjs --owned --rederived-only`, as
// a file. A path is never classified here. That mode is narrower than `--owned`:
// only rules whose `rederivedByCheck` claims such a check. A lockfile is
// generated but nothing regenerates it in CI, so it stays visible. A repository
// declaring no rules gets an empty list and an untouched diff.
//
// Reads the diff on stdin, writes the filtered diff on stdout, reports what it
// dropped on stderr. Fails OPEN: an unparsable section header is KEPT, because
// keeping a generated file costs review budget and dropping a hand-written one
// costs an unread change.
//
// Usage: node .github/scripts/strip-generated-diff.mjs <omit-list-file> < diff
// where <omit-list-file> holds one exact path per line.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

// Where a section STARTS. Every `diff --git` line begins one, whatever its
// paths look like, so splitting never depends on parsing them.
const SECTION_START = /^diff --git /u;

// A header git wrote for a path with no whitespace. A quoted or space-bearing
// path does not match, so its section is kept — see the fail-open note above.
const HEADER = /^diff --git a\/(?<before>\S+) b\/(?<after>\S+)$/u;

/** True when `path` is one the omit list vouches for. EXACT match only: a
 * directory would hide a file its generator never emits, which no check the
 * flag names has to catch. resolve-generated.mjs refuses that rule too. */
const isOmitted = (path, omit) => omit.includes(path);

/** Split a unified diff into sections, each starting at a `diff --git` line.
 * A leading chunk before the first one (git emits none, but a caller may
 * prepend text) becomes its own always-kept section.
 *
 * Splits on SECTION_START, never on HEADER: a file whose header HEADER cannot
 * read must become its OWN section, or it is appended to the section above it
 * and shares that section\'s fate. A space-bearing source file listed after an
 * omitted artifact would then be dropped with it — the fail-CLOSED outcome this
 * script exists to avoid. */
function splitSections(diff) {
  const lines = diff.split("\n");
  /** @type {string[][]} */
  const sections = [];
  for (const line of lines) {
    if (SECTION_START.test(line) || sections.length === 0) sections.push([]);
    sections[sections.length - 1].push(line);
  }
  return sections.map((s) => s.join("\n"));
}

/** The filtered diff and the paths dropped from it.
 * @param {string} diff a unified diff
 * @param {string[]} omit paths and path prefixes a required check re-derives
 * @returns {{ kept: string, dropped: {path: string, lines: number}[] }} */
export function stripGenerated(diff, omit) {
  /** @type {{path: string, lines: number}[]} */
  const dropped = [];
  const kept = [];
  for (const section of splitSections(diff)) {
    const header = HEADER.exec(section.split("\n", 1)[0]);
    // Both sides must be generated, so a rename that moves a hand-written file
    // onto a generated path (or the reverse) stays in the review.
    const { before, after } = header?.groups ?? {};
    if (header && isOmitted(before, omit) && isOmitted(after, omit)) {
      dropped.push({ path: after, lines: section.split("\n").length });
      continue;
    }
    kept.push(section);
  }
  return { kept: kept.join("\n"), dropped };
}

/** The note prepended to a filtered diff, so the reviewer knows the artifacts
 * changed and why it is not being shown them. Empty when nothing was dropped —
 * an unfiltered diff must pass through byte for byte. */
export function omissionNote(dropped) {
  if (dropped.length === 0) return "";
  const rows = dropped.map(
    (d) => `#   ${d.path} (${d.lines} diff lines, omitted)`,
  );
  return [
    `# NOTE: ${dropped.length} generated file(s) are omitted from the diff below.`,
    "# Each is a build output whose rule in config/auto-resolve-regen-rules.json",
    "# sets rederivedByCheck, asserting a required check re-derives it and fails",
    "# on any difference — so it cannot disagree with the sources shown here. A",
    "# generated file with no such check (a lockfile) is NOT omitted. Review the",
    "# sources.",
    ...rows,
    "",
    "",
  ].join("\n");
}

function main(omitListFile) {
  const omit = readFileSync(omitListFile, "utf8").split("\n").filter(Boolean);
  const prefix = omit.find((entry) => entry.endsWith("/"));
  if (prefix) {
    process.stderr.write(
      `strip-generated-diff: ${prefix} is a directory; the omit list takes exact paths only\n`,
    );
    process.exit(1);
  }
  const diff = readFileSync(0, "utf8");
  const { kept, dropped } = stripGenerated(diff, omit);
  process.stdout.write(
    dropped.length === 0 ? diff : omissionNote(dropped) + kept,
  );
  for (const d of dropped)
    process.stderr.write(
      `strip-generated-diff: omitted ${d.path} (${d.lines} lines)\n`,
    );
}

// Only when RUN, not when the test suite imports the pure functions above.
if (
  process.argv[1] &&
  fileURLToPath(import.meta.url) === resolve(process.argv[1])
)
  main(process.argv[2]);
