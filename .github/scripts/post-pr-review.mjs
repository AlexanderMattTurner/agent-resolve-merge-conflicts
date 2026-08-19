// Turn the review agent's structured findings (review.json) into ONE GitHub PR
// review — always a COMMENT, since the merge consequence lives in the
// review-findings status gate rather than the review event — with inline,
// line-anchored comments plus a summary body, for `gh api` to POST.
//
// Each finding names a (path, line, side). A comment on a line that is not part
// of the diff makes the whole reviews API call 422, so this parses the
// (sanitized) diff to learn which (path, line) positions are actually
// commentable on each side. An unanchorable finding is never dropped: a GATING
// one takes a synthetic anchor so it still opens a resolvable thread, and a
// non-gating one moves into the summary body. Line numbers survive Layer-1
// sanitization (it edits within lines, never adds/removes them), so the sanitized
// diff is a faithful anchor source.
//
// One deterministic recovery before spilling: the reviewer reads diff.txt
// through a numbered view (Read shows the DIFF file's own 1-based line numbers),
// and models routinely echo those instead of the NEW-file numbers the anchoring
// rules demand. When a finding's (path, line) is not commentable but `line`,
// read as an index into diff.txt, lands on a content line of the SAME path,
// that path agreement is strong evidence of a diff-view number — remap it to
// the line's real file-side coordinates so the finding posts inline.
//
// Contract with the caller: prints `PAYLOAD` on stdout when it wrote a payload
// to post, or `SKIP` (exit 0) when the reviewer ran but produced nothing to post
// (a valid review.json with no findings and no summary). A MISSING
// or unparsable review.json means the reviewer crashed before writing its
// output, so this exits NON-ZERO (fail loud) instead of masquerading as a clean
// pass with no review posted. Diagnostics go to stderr.
import { readFileSync, writeFileSync } from "node:fs";
import { sanitize } from "agent-input-sanitizer";
import { readRunCost, formatDollars, plansLine } from "./lib-review-cost.mjs";

// The review text is MODEL output derived from the (untrusted) PR diff, so run
// every string bound for a posted GitHub comment through the same Layer-1
// sanitizer the diff went through on the way in — stripping invisible/format
// (Cf) characters and ANSI escapes so a hidden payload the model echoed from the
// diff cannot ride into the posted review. Layer 1 leaves visible bytes (code,
// markdown, emoji) untouched, so it never corrupts a legitimate suggestion.
async function scrub(text) {
  if (typeof text !== "string" || !text) return text;
  const { cleaned } = await sanitize(text, { html: false });
  return cleaned;
}

const dir = process.env.PR_INPUT_DIR;
if (!dir) throw new Error("PR_INPUT_DIR required");
const commitId = process.env.HEAD_SHA || "";

const payloadPath = `${dir}/review-payload.json`;
const summaryPath = `${dir}/review-summary.txt`;

function skip(msg) {
  process.stdout.write("SKIP\n");
  process.stderr.write(`::warning::${msg}\n`);
  process.exit(0);
}

// A missing or unparsable review.json is not "nothing to review" — the reviewer
// is instructed to always write its verdict there, so its absence means the agent
// crashed before producing one. Fail loud (non-zero exit) so the job goes RED
// instead of silently reporting a clean pass with no review posted; the caller
// (post-pr-review.sh) turns this non-zero exit into a red step.
function fail(msg) {
  process.stderr.write(`::error::${msg}\n`);
  process.exit(1);
}

// A compact cost footnote: the review's API-equivalent cost, plus (via
// plansLine) how many PRs/week that rate sustains on a Max 20x plan — the
// budget-relative signal a single percentage used to carry, in the form a reader
// actually reasons about.
function costFooter() {
  const { cost, model } = readRunCost();
  if (typeof cost !== "number" || !Number.isFinite(cost) || cost < 0) return "";
  const modelLabel = model ? ` (${model})` : "";
  const costLine = `<sub>📊 Review cost: **$${formatDollars(cost)}**${modelLabel}.</sub>`;
  return [costLine, plansLine(cost)].filter(Boolean).join("\n");
}

let review;
try {
  review = JSON.parse(readFileSync(`${dir}/review.json`, "utf8"));
} catch (err) {
  // A missing verdict has two very different causes, told apart by the run's
  // total cost (the model is only billed once it is actually reached):
  //   cost > 0  → the reviewer RAN and crashed before writing its verdict. A
  //              real bug — fail loud so the job goes RED.
  //   cost 0/absent → the model was NEVER reached: no CLAUDE_CODE_OAUTH_TOKEN is
  //              configured, or the token is expired/rate-limited. The reviewer is
  //              an OPTIONAL feature; an unconfigured one must not red every PR, so
  //              skip with a visible warning pointing at the fix.
  const { cost } = readRunCost();
  const ran = typeof cost === "number" && Number.isFinite(cost) && cost > 0;
  const base = `the reviewer wrote no valid review.json (${err.message})`;
  if (ran) {
    fail(
      `${base} — it ran (cost $${cost.toFixed(4)}) but crashed before producing its verdict`,
    );
  }
  skip(
    `${base}; run cost is zero/absent, so the reviewer never reached the model — ` +
      "no CLAUDE_CODE_OAUTH_TOKEN configured, or an expired/rate-limited token. " +
      "Configure the credential to enable PR reviews.",
  );
}

const findings = Array.isArray(review.findings) ? review.findings : [];
const summary = typeof review.summary === "string" ? review.summary.trim() : "";

// Every review posts as a COMMENT, and the review event carries no merge
// consequence. The merge lever is the inline threads review_findings_gate.py
// reads. review.json's `verdict` field is advisory prose the reviewer folds into
// its own summary; nothing here acts on it.
const event = "COMMENT";

// The severity model is CONFIG, not code: config/review-severities.json says
// which severities hold the merge and what emoji each finding leads with. It is
// the same SSOT review_findings_gate.py reads, so the stamper and the gate cannot
// drift on what gates.
//
// `gating` lists the severities whose unresolved threads hold the merge. A gating
// finding therefore ALWAYS opens a thread: one that cannot anchor gets a
// synthetic anchor below, because the gate reads only threads and a gating
// finding spilled into the review body would ride through unresolvable.
//
// The shipped model. `config/` is NOT in template-sync's SYNC_PATHS, so an
// adopter repo receives this script without the config file — absent therefore
// means "kept the shipped defaults", exactly as config/pr-review-paths.json is
// read. Treating absent as fatal would red every review in every repo that
// syncs this script, since the file cannot arrive.
const DEFAULT_SEVERITIES = {
  gating: ["blocking", "warning"],
  icons: { blocking: "🔴", warning: "🟡", nit: "🔵" },
};

// Read relative to this script, not the repo root. The reviewer runs against an
// untrusted PR head, so a root-relative lookup would let a PR author's copy
// decide which of its own findings hold the merge.
const SEVERITY_CONFIG = new URL(
  "../../config/review-severities.json",
  import.meta.url,
);

const normSeverity = (s) =>
  typeof s === "string" ? s.trim().toLowerCase() : "";

// Absent is a choice; malformed is a mistake. A repo that never wrote the file
// gets DEFAULT_SEVERITIES. A repo that DID write one and got it wrong fails loud,
// because the failure it would otherwise cause is invisible: an empty `gating` set
// gives every finding a spillable severity, so a real one lands in the review body
// with no thread for the gate to hold — indistinguishable from a clean review.
function loadSeverities() {
  let text;
  try {
    text = readFileSync(SEVERITY_CONFIG, "utf8");
  } catch (err) {
    if (err.code !== "ENOENT") throw err;
    return {
      gating: new Set(DEFAULT_SEVERITIES.gating),
      icons: new Map(Object.entries(DEFAULT_SEVERITIES.icons)),
    };
  }
  let raw;
  try {
    raw = JSON.parse(text);
  } catch (err) {
    throw new Error(
      `config/review-severities.json is unparsable (${err.message}); refusing to ` +
        "review with a severity model somebody meant to set and got wrong.",
    );
  }
  const bad = (why) => new Error(`config/review-severities.json: ${why}`);
  const isPlainObject = (v) =>
    typeof v === "object" && v !== null && !Array.isArray(v);
  if (!isPlainObject(raw)) throw bad("the top level must be a JSON object.");
  if (!isPlainObject(raw.icons))
    throw bad("`icons` must be an object mapping each severity to its emoji.");
  const icons = new Map(Object.entries(raw.icons));
  for (const [sev, glyph] of icons)
    if (typeof glyph !== "string" || !glyph)
      throw bad(`severity '${sev}' has a non-string icon.`);
  if (!Array.isArray(raw.gating) || raw.gating.length === 0)
    throw bad(
      "`gating` must be a non-empty array — an empty one lets every finding " +
        "spill into the review body, where nothing holds the merge.",
    );
  const gating = new Set();
  for (const sev of raw.gating) {
    if (typeof sev !== "string" || !sev)
      throw bad("every `gating` entry must be a non-empty string.");
    if (!icons.has(sev))
      throw bad(`gating severity '${sev}' has no entry in \`icons\`.`);
    gating.add(sev);
  }
  return { gating, icons };
}

const { gating: GATING_SEVERITIES, icons: ICONS } = loadSeverities();

// Commentable (path, line) positions per side, parsed from the unified diff.
// Context lines are commentable on both sides; added lines on RIGHT, removed on
// LEFT. diffViewLines maps each 1-based physical line of diff.txt to the file
// coordinates of the content line there — the anchor space for the diff-view
// remap.
const rightOk = new Set();
const leftOk = new Set();
const diffViewLines = [];
// The first RIGHT-side line of each path, and of the diff overall — the synthetic
// anchor a gating finding that names no diff line is attached to.
const firstRightByPath = new Map();
let firstRightOverall = null;
let path = null;
let oldLine = 0;
let newLine = 0;
const diffLines = readFileSync(`${dir}/diff.txt`, "utf8").split("\n");
for (let i = 0; i < diffLines.length; i++) {
  const raw = diffLines[i];
  if (raw.startsWith("--- ")) continue;
  if (raw.startsWith("+++ ")) {
    const target = raw.slice(4);
    const m = target.match(/^b\/(?<path>.*)$/);
    path = m ? m.groups.path : target;
    continue;
  }
  if (raw.startsWith("@@")) {
    const m = raw.match(
      /@@ -(?<oldStart>\d+)(?:,\d+)? \+(?<newStart>\d+)(?:,\d+)? @@/,
    );
    if (m) {
      oldLine = Number.parseInt(m.groups.oldStart, 10);
      newLine = Number.parseInt(m.groups.newStart, 10);
    }
    continue;
  }
  if (path === null) continue;
  const kind = raw[0];
  if (kind === "+") {
    rightOk.add(`${path}\t${newLine}`);
    if (!firstRightByPath.has(path)) firstRightByPath.set(path, newLine);
    if (!firstRightOverall) firstRightOverall = { path, line: newLine };
    diffViewLines[i + 1] = { path, kind, newLine, oldLine: null };
    newLine += 1;
  } else if (kind === "-") {
    leftOk.add(`${path}\t${oldLine}`);
    diffViewLines[i + 1] = { path, kind, newLine: null, oldLine };
    oldLine += 1;
  } else if (kind === " ") {
    rightOk.add(`${path}\t${newLine}`);
    if (!firstRightByPath.has(path)) firstRightByPath.set(path, newLine);
    if (!firstRightOverall) firstRightOverall = { path, line: newLine };
    leftOk.add(`${path}\t${oldLine}`);
    diffViewLines[i + 1] = { path, kind, newLine, oldLine };
    oldLine += 1;
    newLine += 1;
  }
}

// Recover a diff-view anchor (see header): remap viewLine — a 1-based line
// number of diff.txt itself — to the file-side coordinates of the content line
// at that position, but ONLY when that line belongs to the finding's own path
// (the evidence the number was a diff-view index and not a hallucination).
// A removed line anchors LEFT-only, and a suggestion is RIGHT-only, so a
// suggestion cannot ride a '-' remap.
function remapDiffViewAnchor(findingPath, viewLine, hasSuggestion) {
  const m = diffViewLines[viewLine];
  if (!m || m.path !== findingPath) return null;
  if (m.kind === "-")
    return hasSuggestion ? null : { line: m.oldLine, side: "LEFT" };
  return { line: m.newLine, side: "RIGHT" };
}

// Normalized the same way the gate normalizes, so a severity that HOLDS the
// merge always renders the glyph its hold is named after — a cased "Blocking"
// that gated but posted the "•" fallback would read as an unclassified note.
const icon = (sev) => ICONS.get(normSeverity(sev)) || "•";

// The hidden marker review_findings_gate.py keys on. Only a severity the icon map
// renders is stamped, so the gate never learns one it cannot read.
const severityMarker = (sev) =>
  ICONS.has(normSeverity(sev))
    ? `\n\n<!-- severity: ${normSeverity(sev)} -->`
    : "";

// A `suggestion` renders as a GitHub suggested-change block the author can apply
// with one click. Suggestions can only target the new file (RIGHT side), so a
// finding carrying one is forced RIGHT. A fence longer than any run of backticks
// in the suggestion keeps code containing ``` from breaking out of the block.
function suggestionBlock(text) {
  const longest = Math.max(
    0,
    ...(text.match(/`+/g) || []).map((run) => run.length),
  );
  const fence = "`".repeat(Math.max(3, longest + 1));
  return `\n\n${fence}suggestion\n${text}\n${fence}`;
}

const commentableRight = (p, l) => l !== null && rightOk.has(`${p}\t${l}`);

const comments = [];
const spill = [];
for (const f of findings) {
  const detail = [f.title, f.body].filter(Boolean).join(" — ").trim();
  // A detail-less finding is dropped and never gates: there is nothing to resolve.
  if (!detail) continue;
  const sev = normSeverity(f.severity);
  const line = Number.isInteger(f.line) ? f.line : null;
  const hasSuggestion =
    typeof f.suggestion === "string" && f.suggestion.length > 0;
  const side = hasSuggestion || f.side !== "LEFT" ? "RIGHT" : "LEFT";
  const ok = side === "LEFT" ? leftOk : rightOk;

  // The anchor actually posted: the finding's own (line, side) when commentable,
  // else the diff-view remap's recovery. start_line is remapped through the same
  // coordinate space as its line — mixing a remapped line with a literal start
  // would anchor a range that never existed.
  let anchorLine = line;
  let anchorSide = side;
  let start = Number.isInteger(f.start_line) ? f.start_line : null;
  if (f.path && line && !ok.has(`${f.path}\t${line}`)) {
    const remap = remapDiffViewAnchor(f.path, line, hasSuggestion);
    if (remap) {
      anchorLine = remap.line;
      anchorSide = remap.side;
      if (start) {
        const remapStart = remapDiffViewAnchor(f.path, start, false);
        start =
          remapStart && remapStart.side === "RIGHT" ? remapStart.line : null;
      }
    }
  }
  const anchorOk = anchorSide === "LEFT" ? leftOk : rightOk;

  if (f.path && anchorLine && anchorOk.has(`${f.path}\t${anchorLine}`)) {
    const comment = {
      path: f.path,
      line: anchorLine,
      side: anchorSide,
      body: `${icon(sev)} ${detail}`,
    };
    // Multi-line suggestion/anchor: keep it only when the whole RIGHT-side range
    // is in the diff, else GitHub 422s the review.
    if (
      start &&
      start < anchorLine &&
      anchorSide === "RIGHT" &&
      commentableRight(f.path, start)
    ) {
      comment.start_line = start;
      comment.start_side = "RIGHT";
    }
    if (hasSuggestion && anchorSide === "RIGHT")
      comment.body += suggestionBlock(f.suggestion);
    comment.body += severityMarker(sev);
    comments.push(comment);
  } else {
    const where = f.path
      ? `\`${f.path}${line ? `:${line}` : ""}\``
      : "(general)";
    // A GATING finding that cannot anchor gets a synthetic anchor: the gate reads
    // only threads, so spilling it into the review body would let it ride through
    // unresolvable — the one way this reviewer could silently lose its hold on a
    // merge. The body says so, and no suggestion rides a synthetic anchor, since it
    // would edit a line the finding is not about. Only nits spill.
    const synthetic =
      GATING_SEVERITIES.has(sev) &&
      (f.path && firstRightByPath.has(f.path)
        ? { path: f.path, line: firstRightByPath.get(f.path) }
        : firstRightOverall);
    if (synthetic) {
      comments.push({
        path: synthetic.path,
        line: synthetic.line,
        side: "RIGHT",
        body: `${icon(sev)} ${detail}\n\n<sub>PR-wide finding at ${where}: it names no line in this diff, so it is anchored here to open a resolvable thread.</sub>${severityMarker(sev)}`,
      });
    } else {
      // No RIGHT-side line exists anywhere in this diff — a PR that only deletes
      // files, or only changes binary ones — so the synthetic anchor has nowhere to
      // go and a GATING finding spills into the body, where the threads-only gate
      // cannot see it. The anchor genuinely cannot exist, so the honest posture is
      // loud: name the finding that holds nothing, rather than let the run read clean.
      if (GATING_SEVERITIES.has(sev))
        console.error(
          `::error::gating finding at ${where} could not be anchored anywhere in this diff — it opens no thread, so it holds nothing`,
        );
      spill.push(`- ${icon(sev)} ${where}: ${detail}`);
    }
  }
}

// Sanitize the model-authored strings before they reach the payload: each inline
// comment body (which already carries its suggestion block) and the composite
// summary/spill body.
for (const c of comments) c.body = await scrub(c.body);

const bodyParts = [];
if (summary) bodyParts.push(summary);
if (spill.length > 0)
  bodyParts.push(`#### Additional notes\n${spill.join("\n")}`);
const body = (await scrub(bodyParts.join("\n\n"))).trim();

// A review with nothing to say is noise, so skip it. Nothing is lost: a gating
// finding always becomes a comment (synthetic anchor above), so an empty comment
// list plus an empty body means the reviewer really found nothing, and the caller
// still posts no review — leaving the findings gate to wait on the next read.
if (comments.length === 0 && !body)
  skip("reviewer produced no findings and no summary");

const footer = costFooter();
const postedBody =
  [body, footer].filter(Boolean).join("\n\n---\n") || "Automated review.";

const payload = {
  event,
  body: postedBody,
  comments,
};
if (commitId) payload.commit_id = commitId;

writeFileSync(payloadPath, JSON.stringify(payload));
writeFileSync(summaryPath, postedBody);
process.stdout.write("PAYLOAD\n");
process.stderr.write(
  `inline comments: ${comments.length}; spilled to summary: ${spill.length}\n`,
);
