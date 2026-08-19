// Behavior tests for post-pr-review.mjs: run the real script over a temp
// PR_INPUT_DIR (diff.txt + review.json) and assert on the reviews-API payload it
// emits — anchor validation, suggested-edit rendering, the severity marker the
// review-findings gate reads, the synthetic anchor a gating finding gets when it
// names no diff line, the summary spill path, the SKIP paths, and the fail-loud
// path (a crashed reviewer that wrote no review.json exits non-zero). Drives the
// script as a subprocess (its real entry point), never re-implements its logic.
import { describe, it, afterEach } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdirSync,
  writeFileSync,
  readFileSync,
  copyFileSync,
  symlinkSync,
  existsSync,
  rmSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { scratchDir } from "./lib-test-scratch.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCRIPT = join(__dirname, "post-pr-review.mjs");

// A unified diff for src/foo.js whose one hunk yields these commentable lines:
//   RIGHT (new file): 1, 2, 3, 4, 5      LEFT (old file): 1, 2, 3, 4
// Line 5 is RIGHT-only (a context line whose old-side number, 4, differs), which
// lets a test prove a suggestion forces the RIGHT side.
const DIFF = `diff --git a/src/foo.js b/src/foo.js
index 1111111..2222222 100644
--- a/src/foo.js
+++ b/src/foo.js
@@ -1,4 +1,5 @@
 const a = 1;
-const b = 2;
+const b = 3;
+const c = 4;
 const d = 5;
 const e = 6;
`;

const dirs = [];
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop(), { recursive: true, force: true });
});

// Run the poster over a temp dir seeded with `review` (object) and a diff
// (default DIFF). Returns { status, stderr, payload, summary }; payload/summary are
// null when no payload file was written.
function run(review, { diff = DIFF, headSha, executionFile, maxWeekly } = {}) {
  const dir = scratchDir("prr-");
  dirs.push(dir);
  writeFileSync(join(dir, "diff.txt"), diff);
  writeFileSync(
    join(dir, "review.json"),
    typeof review === "string" ? review : JSON.stringify(review),
  );
  // Neutralize the cost footer by default so body assertions are deterministic:
  // clear both the explicit EXECUTION_FILE and the RUNNER_TEMP fallback path
  // (CI runners set RUNNER_TEMP, which would otherwise be probed). Footer tests
  // opt back in via the executionFile option.
  const env = { ...process.env, PR_INPUT_DIR: dir, EXECUTION_FILE: "" };
  delete env.RUNNER_TEMP;
  if (headSha !== undefined) env.HEAD_SHA = headSha;
  if (executionFile !== undefined) env.EXECUTION_FILE = executionFile;
  if (maxWeekly !== undefined) env.MAX20X_WEEKLY_USD = maxWeekly;
  // spawnSync so a test can read the annotations the script writes to stderr; the
  // non-zero check below keeps execFileSync's fail-loud behavior.
  const res = spawnSync("node", [SCRIPT], { env, encoding: "utf8" });
  if (res.status !== 0)
    throw new Error(`post-pr-review exited ${res.status}: ${res.stderr}`);
  const status = res.stdout.trim();
  const payloadPath = join(dir, "review-payload.json");
  const summaryPath = join(dir, "review-summary.txt");
  return {
    status,
    stderr: res.stderr,
    payload: existsSync(payloadPath)
      ? JSON.parse(readFileSync(payloadPath, "utf8"))
      : null,
    summary: existsSync(summaryPath) ? readFileSync(summaryPath, "utf8") : null,
  };
}

describe("post-pr-review: anchored inline comments", () => {
  it("renders a single-line finding with a suggestion block", () => {
    const { status, payload } = run({
      summary: "needs changes",
      findings: [
        {
          path: "src/foo.js",
          line: 2,
          side: "RIGHT",
          severity: "warning",
          title: "bug",
          body: "wrong value",
          suggestion: "const b = 4;",
        },
      ],
    });
    assert.equal(status, "PAYLOAD");
    assert.equal(payload.comments.length, 1);
    const c = payload.comments[0];
    assert.equal(c.path, "src/foo.js");
    assert.equal(c.line, 2);
    assert.equal(c.side, "RIGHT");
    assert.equal(c.start_line, undefined);
    // The severity marker lands LAST, after the suggestion block: the gate reads
    // it as a whole line of the thread's root body.
    assert.equal(
      c.body,
      "🟡 bug — wrong value\n\n```suggestion\nconst b = 4;\n```" +
        "\n\n<!-- severity: warning -->",
    );
  });

  it("carries start_line/start_side for a multi-line suggestion", () => {
    const { payload } = run({
      summary: "s",
      findings: [
        {
          path: "src/foo.js",
          line: 3,
          start_line: 2,
          side: "RIGHT",
          severity: "nit",
          title: "t",
          body: "b",
          suggestion: "const b = 3;\nconst c = 5;",
        },
      ],
    });
    const c = payload.comments[0];
    assert.equal(c.line, 3);
    assert.equal(c.start_line, 2);
    assert.equal(c.start_side, "RIGHT");
    assert.match(c.body, /```suggestion\nconst b = 3;\nconst c = 5;\n```/);
  });

  it("comments on a removed line via the LEFT side", () => {
    const { payload } = run({
      summary: "s",
      findings: [
        {
          path: "src/foo.js",
          line: 2,
          side: "LEFT",
          severity: "nit",
          title: "t",
          body: "b",
        },
      ],
    });
    assert.equal(payload.comments.length, 1);
    assert.equal(payload.comments[0].side, "LEFT");
    assert.doesNotMatch(payload.comments[0].body, /suggestion/);
  });

  it("forces RIGHT when a finding carries a suggestion", () => {
    // side LEFT + line 5: 5 is RIGHT-only, so this only anchors if forced RIGHT.
    const { payload } = run({
      summary: "s",
      findings: [
        {
          path: "src/foo.js",
          line: 5,
          side: "LEFT",
          severity: "warning",
          title: "t",
          body: "b",
          suggestion: "const e = 7;",
        },
      ],
    });
    assert.equal(payload.comments.length, 1);
    assert.equal(payload.comments[0].side, "RIGHT");
    assert.match(payload.comments[0].body, /```suggestion/);
  });

  it("uses a longer fence when the suggestion contains backticks", () => {
    const { payload } = run({
      summary: "s",
      findings: [
        {
          path: "src/foo.js",
          line: 4,
          side: "RIGHT",
          severity: "nit",
          title: "t",
          body: "b",
          suggestion: "a ``` b",
        },
      ],
    });
    assert.match(payload.comments[0].body, /````suggestion\na ``` b\n````/);
  });
});

describe("post-pr-review: diff-view anchor remap", () => {
  // In DIFF the physical lines of diff.txt are: 1-5 headers/hunk, then content:
  //   6 ` const a = 1;` (ctx, new 1)   7 `-const b = 2;` (old 2)
  //   8 `+const b = 3;` (new 2)        9 `+const c = 4;` (new 3)
  //   10 ` const d = 5;` (new 4)       11 ` const e = 6;` (new 5)
  // Views 6-11 never collide with the commentable new-file lines 1-5, so a
  // finding carrying a view number is unambiguously un-anchorable pre-remap.

  it("remaps a diff-file line number to the real new-file line", () => {
    const { payload } = run({
      summary: "s",
      findings: [
        {
          path: "src/foo.js",
          line: 8,
          side: "RIGHT",
          severity: "blocking",
          title: "t",
          body: "b",
        },
      ],
    });
    assert.equal(payload.comments.length, 1);
    assert.equal(payload.comments[0].line, 2);
    assert.equal(payload.comments[0].side, "RIGHT");
    assert.doesNotMatch(payload.body, /Additional notes/);
  });

  it("keeps a suggestion riding a remapped added-line anchor", () => {
    const { payload } = run({
      summary: "s",
      findings: [
        {
          path: "src/foo.js",
          line: 9,
          side: "RIGHT",
          severity: "warning",
          title: "t",
          body: "b",
          suggestion: "const c = 5;",
        },
      ],
    });
    assert.equal(payload.comments.length, 1);
    assert.equal(payload.comments[0].line, 3);
    assert.match(payload.comments[0].body, /```suggestion\nconst c = 5;\n```/);
  });

  it("remaps a removed-line diff-view number to the LEFT side", () => {
    const { payload } = run({
      summary: "s",
      findings: [
        {
          path: "src/foo.js",
          line: 7,
          side: "RIGHT",
          severity: "nit",
          title: "t",
          body: "b",
        },
      ],
    });
    assert.equal(payload.comments.length, 1);
    assert.equal(payload.comments[0].line, 2);
    assert.equal(payload.comments[0].side, "LEFT");
  });

  it("spills a suggestion pointed at a removed diff-view line (RIGHT-only)", () => {
    // A NIT, so the spill is observable: a gating severity takes the synthetic
    // anchor instead (see the gating-thread suite below).
    const { payload } = run({
      summary: "s",
      findings: [
        {
          path: "src/foo.js",
          line: 7,
          side: "RIGHT",
          severity: "nit",
          title: "t",
          body: "b",
          suggestion: "const b = 9;",
        },
      ],
    });
    assert.equal(payload.comments.length, 0);
    assert.match(payload.body, /`src\/foo\.js:7`: t — b/);
  });

  it("remaps start_line through the same coordinate space", () => {
    const { payload } = run({
      summary: "s",
      findings: [
        {
          path: "src/foo.js",
          line: 9,
          start_line: 8,
          side: "RIGHT",
          severity: "warning",
          title: "t",
          body: "b",
          suggestion: "const b = 3;\nconst c = 5;",
        },
      ],
    });
    const c = payload.comments[0];
    assert.equal(c.line, 3);
    assert.equal(c.start_line, 2);
    assert.equal(c.start_side, "RIGHT");
  });

  it("drops an unremappable start_line but still posts the remapped line", () => {
    // start_line 7 is a removed line: it can only remap LEFT, so it cannot open
    // a RIGHT-side range — the comment posts single-line at the remapped anchor.
    const { payload } = run({
      summary: "s",
      findings: [
        {
          path: "src/foo.js",
          line: 9,
          start_line: 7,
          side: "RIGHT",
          severity: "warning",
          title: "t",
          body: "b",
        },
      ],
    });
    const c = payload.comments[0];
    assert.equal(c.line, 3);
    assert.equal(c.start_line, undefined);
  });

  it("does not remap across paths: a view line in another file's hunk spills", () => {
    // Two-file diff: view line 14 is bar.js content (new-file line 2). Claimed
    // under foo.js it must spill, not anchor to the wrong file's coordinates;
    // claimed under bar.js it remaps.
    const twoFileDiff = `diff --git a/src/foo.js b/src/foo.js
index 1111111..2222222 100644
--- a/src/foo.js
+++ b/src/foo.js
@@ -1,1 +1,2 @@
 const a = 1;
+const b = 3;
diff --git a/src/bar.js b/src/bar.js
index 3333333..4444444 100644
--- a/src/bar.js
+++ b/src/bar.js
@@ -1,1 +1,2 @@
 const x = 1;
+const y = 2;
`;
    const mismatch = run(
      {
        summary: "s",
        findings: [
          {
            path: "src/foo.js",
            line: 14,
            side: "RIGHT",
            severity: "nit",
            title: "t",
            body: "b",
          },
        ],
      },
      { diff: twoFileDiff },
    );
    assert.equal(mismatch.payload.comments.length, 0);
    assert.match(mismatch.payload.body, /`src\/foo\.js:14`: t — b/);

    const match = run(
      {
        summary: "s",
        findings: [
          {
            path: "src/bar.js",
            line: 14,
            side: "RIGHT",
            severity: "warning",
            title: "t",
            body: "b",
          },
        ],
      },
      { diff: twoFileDiff },
    );
    assert.equal(match.payload.comments.length, 1);
    assert.equal(match.payload.comments[0].path, "src/bar.js");
    assert.equal(match.payload.comments[0].line, 2);
  });
});

describe("post-pr-review: severity icons and markers", () => {
  // A severity the icon map does not render gets the "•" fallback and NO marker:
  // the gate keys on the marker, so stamping one it cannot read would be worse
  // than stamping none.
  for (const [severity, expected, marker] of [
    ["blocking", "🔴", "\n\n<!-- severity: blocking -->"],
    ["warning", "🟡", "\n\n<!-- severity: warning -->"],
    ["nit", "🔵", "\n\n<!-- severity: nit -->"],
    ["bogus", "•", ""],
  ]) {
    it(`maps ${severity} to ${expected}`, () => {
      const { payload } = run({
        summary: "s",
        findings: [
          {
            path: "src/foo.js",
            line: 1,
            side: "RIGHT",
            severity,
            title: "t",
            body: "b",
          },
        ],
      });
      assert.equal(payload.comments[0].body, `${expected} t — b${marker}`);
    });
  }

  it("normalizes a cased severity for both the icon and the marker", () => {
    const { payload } = run({
      summary: "s",
      findings: [
        {
          path: "src/foo.js",
          line: 1,
          side: "RIGHT",
          severity: " Warning ",
          title: "t",
          body: "b",
        },
      ],
    });
    assert.equal(
      payload.comments[0].body,
      "🟡 t — b\n\n<!-- severity: warning -->",
    );
  });
});

describe("post-pr-review: summary + spill", () => {
  it("spills an un-anchorable NIT into Additional notes, not comments", () => {
    const { payload } = run({
      summary: "verdict line",
      findings: [
        {
          path: "src/foo.js",
          line: 999,
          side: "RIGHT",
          severity: "nit",
          title: "t",
          body: "b",
        },
      ],
    });
    assert.equal(payload.comments.length, 0);
    assert.match(payload.body, /^verdict line/);
    assert.match(payload.body, /#### Additional notes/);
    assert.match(payload.body, /`src\/foo\.js:999`: t — b/);
  });

  it("posts a summary-only review when there are no findings", () => {
    const { status, payload } = run({ summary: "looks good", findings: [] });
    assert.equal(status, "PAYLOAD");
    assert.deepEqual(payload.comments, []);
    assert.equal(payload.body, "looks good");
  });

  it("falls back to a placeholder body when comments exist but summary is empty", () => {
    const { payload } = run({
      summary: "",
      findings: [
        {
          path: "src/foo.js",
          line: 1,
          side: "RIGHT",
          severity: "nit",
          title: "t",
          body: "b",
        },
      ],
    });
    assert.equal(payload.comments.length, 1);
    assert.equal(payload.body, "Automated review.");
  });
});

describe("post-pr-review: the review event is always COMMENT", () => {
  // The merge consequence lives in the "Review findings resolved" status check,
  // which reads the finding THREADS. A review that voted would add a second,
  // stickier lever nothing clears: a submitted CHANGES_REQUESTED survives every
  // thread resolution.
  for (const verdict of [
    "looks_good",
    "needs_changes",
    "blocking",
    "LOOKS_GOOD",
    " Blocking ",
    "bogus",
    "",
    undefined,
  ]) {
    it(`posts COMMENT for a ${JSON.stringify(verdict)} verdict`, () => {
      const review = {
        summary: "please fix",
        findings: [
          {
            path: "src/foo.js",
            line: 2,
            side: "RIGHT",
            severity: "blocking",
            title: "t",
            body: "b",
          },
        ],
      };
      if (verdict !== undefined) review.verdict = verdict;
      const { payload } = run(review);
      assert.equal(payload.event, "COMMENT");
    });
  }

  it("posts COMMENT for a summary with no findings at all", () => {
    const { payload } = run({ summary: "all good", verdict: "looks_good" });
    assert.equal(payload.event, "COMMENT");
    assert.deepEqual(payload.comments, []);
  });
});

describe("post-pr-review: a gating finding always opens a thread", () => {
  // The gate reads only threads, so a gating finding that spilled into the review
  // body would ride through unresolvable — the one way this reviewer could
  // silently lose its hold on a merge. `line: 999` names no line in DIFF.
  const unanchorable = {
    path: "src/foo.js",
    line: 999,
    side: "RIGHT",
    title: "lax shape",
    body: "a tighter design is available",
  };

  for (const severity of ["blocking", "warning", " Warning "]) {
    it(`anchors an un-anchorable ${severity} finding synthetically`, () => {
      const { payload } = run({
        summary: "design note",
        findings: [{ ...unanchorable, severity }],
      });
      assert.equal(payload.comments.length, 1);
      const c = payload.comments[0];
      // The first RIGHT-side line of the finding's own path in the diff.
      assert.equal(c.path, "src/foo.js");
      assert.equal(c.line, 1);
      assert.equal(c.side, "RIGHT");
      assert.match(c.body, /PR-wide finding at `src\/foo\.js:999`/);
      assert.match(c.body, /<!-- severity: \w+ -->$/);
      assert.doesNotMatch(payload.body, /#### Additional notes/);
    });
  }

  it("anchors a path-less gating finding to the first line of the diff", () => {
    const { payload } = run({
      summary: "PR-wide",
      findings: [
        { severity: "blocking", title: "t", body: "the whole approach" },
      ],
    });
    assert.equal(payload.comments.length, 1);
    assert.equal(payload.comments[0].path, "src/foo.js");
    assert.equal(payload.comments[0].line, 1);
    assert.match(payload.comments[0].body, /PR-wide finding at \(general\)/);
  });

  it("carries no suggestion on a synthetic anchor", () => {
    // The synthetic line is not the line the finding is about, so applying a
    // suggestion there would edit the wrong code.
    const { payload } = run({
      summary: "s",
      findings: [
        { ...unanchorable, severity: "blocking", suggestion: "const z = 1;" },
      ],
    });
    assert.equal(payload.comments.length, 1);
    assert.doesNotMatch(payload.comments[0].body, /```suggestion/);
  });

  // A PR that only DELETES files, or only changes binary ones, has no RIGHT-side
  // line anywhere, so the synthetic anchor has nowhere to go at all.
  const DELETION_ONLY_DIFF = `diff --git a/src/gone.js b/src/gone.js
deleted file mode 100644
index 1111111..0000000
--- a/src/gone.js
+++ /dev/null
@@ -1,3 +0,0 @@
-const a = 1;
-const b = 2;
-const c = 3;
`;

  it("annotates loudly when no RIGHT-side line exists to anchor to", () => {
    // The anchor genuinely cannot exist, so the run must NAME the finding that
    // holds nothing rather than read as a clean review.
    const { payload, stderr } = run(
      {
        summary: "deletion review",
        findings: [
          {
            severity: "blocking",
            title: "t",
            body: "this removal breaks callers",
          },
        ],
      },
      { diff: DELETION_ONLY_DIFF },
    );
    assert.deepEqual(payload.comments, []);
    assert.match(stderr, /::error::gating finding at \(general\)/);
    assert.match(stderr, /it opens no thread, so it holds nothing/);
    assert.match(payload.body, /#### Additional notes/);
  });

  it("stays quiet when the un-anchorable finding is only a nit", () => {
    // A nit was always allowed to spill, so annotating one would train the reader
    // to ignore the annotation that matters.
    const { payload, stderr } = run(
      {
        summary: "deletion review",
        findings: [{ severity: "nit", title: "t", body: "a tidier removal" }],
      },
      { diff: DELETION_ONLY_DIFF },
    );
    assert.deepEqual(payload.comments, []);
    assert.doesNotMatch(stderr, /::error::gating finding/);
  });

  it("lets a NIT spill instead — it holds nothing", () => {
    const { payload } = run({
      summary: "cosmetic",
      findings: [{ ...unanchorable, severity: "nit" }],
    });
    assert.deepEqual(payload.comments, []);
    assert.match(payload.body, /#### Additional notes/);
  });

  it("drops a detail-less finding, so it opens no thread", () => {
    // A finding with no title and no body has nothing for the author to resolve.
    const { status, payload } = run({
      summary: "ok",
      findings: [{ path: "src/foo.js", line: 999, severity: "blocking" }],
    });
    assert.equal(status, "PAYLOAD");
    assert.deepEqual(payload.comments, []);
    assert.equal(payload.body, "ok");
  });
});

describe("post-pr-review: commit pinning", () => {
  it("pins commit_id from HEAD_SHA", () => {
    const { payload } = run(
      { summary: "s", findings: [] },
      { headSha: "abc123" },
    );
    assert.equal(payload.commit_id, "abc123");
  });

  it("omits commit_id when HEAD_SHA is unset", () => {
    const { payload } = run({ summary: "s", findings: [] });
    assert.equal("commit_id" in payload, false);
  });
});

describe("post-pr-review: SKIP paths", () => {
  it("skips when there are no findings and no summary", () => {
    const { status, payload } = run({ summary: "", findings: [] });
    assert.equal(status, "SKIP");
    assert.equal(payload, null);
  });

  it("drops a finding with no title/body", () => {
    const { status, payload } = run({
      summary: "",
      findings: [
        { path: "src/foo.js", line: 1, side: "RIGHT", severity: "nit" },
      ],
    });
    assert.equal(status, "SKIP");
    assert.equal(payload, null);
  });
});

describe("post-pr-review: fail loud on a crashed reviewer", () => {
  // Run the poster expecting a NON-ZERO exit; returns { code, stderr }. A missing
  // or unparsable review.json means the reviewer crashed before writing its
  // verdict — that must go red, not skip green. `writeReview: false` omits
  // review.json entirely (the crash that produced #2366's silent green).
  // The fail-loud path is gated on the run having actually reached the model:
  // a positive total_cost_usd means the reviewer RAN and then crashed (red),
  // whereas zero/absent cost means it never reached the model (unconfigured /
  // credential failure) and skips green. Every fail case therefore writes a
  // cost>0 execution log so it exercises the "ran and crashed" branch.
  function ranExecLog() {
    const dir = scratchDir("prr-exec-");
    dirs.push(dir);
    const path = join(dir, "claude-execution-output.json");
    writeFileSync(
      path,
      JSON.stringify([{ type: "result", total_cost_usd: 0.1 }]),
    );
    return path;
  }

  function runPoster(review, { writeReview = true, executionFile = "" } = {}) {
    const dir = scratchDir("prr-");
    dirs.push(dir);
    writeFileSync(join(dir, "diff.txt"), DIFF);
    if (writeReview)
      writeFileSync(
        join(dir, "review.json"),
        typeof review === "string" ? review : JSON.stringify(review),
      );
    const env = {
      ...process.env,
      PR_INPUT_DIR: dir,
      EXECUTION_FILE: executionFile,
    };
    delete env.RUNNER_TEMP;
    // spawnSync captures stdout AND stderr regardless of exit code, so the
    // skip-path warning (emitted on a zero exit) is observable too.
    const res = spawnSync("node", [SCRIPT], { env, encoding: "utf8" });
    if (res.error) throw res.error;
    return {
      code: res.status,
      stdout: res.stdout ?? "",
      stderr: res.stderr ?? "",
      payload: existsSync(join(dir, "review-payload.json")),
    };
  }

  it("exits non-zero when the reviewer RAN (cost>0) but wrote no review.json", () => {
    const { code, stderr, payload } = runPoster(null, {
      writeReview: false,
      executionFile: ranExecLog(),
    });
    assert.equal(code, 1);
    assert.match(stderr, /::error::/);
    assert.match(stderr, /crashed/);
    assert.equal(payload, false);
  });

  it("exits non-zero on an unparsable review.json when the reviewer ran", () => {
    const { code, stderr, payload } = runPoster("{ not valid json", {
      executionFile: ranExecLog(),
    });
    assert.equal(code, 1);
    assert.match(stderr, /::error::/);
    assert.equal(payload, false);
  });

  it("SKIPS (green) when review.json is missing and run cost is zero/absent — the reviewer never reached the model (unconfigured / credential failure)", () => {
    const { code, stdout, stderr, payload } = runPoster(null, {
      writeReview: false,
      executionFile: "", // no execution log → readRunCost() returns {}
    });
    assert.equal(code, 0);
    assert.match(stdout, /SKIP/);
    assert.match(stderr, /never reached the model/);
    assert.match(stderr, /CLAUDE_CODE_OAUTH_TOKEN/);
    assert.equal(payload, false);
  });
});

describe("post-pr-review: cost footer", () => {
  // Write an execution log shaped like the Claude action's output (an array of
  // streamed events; the terminal `result` event carries total_cost_usd) and
  // return its path, tracked for cleanup.
  function writeExecLog(events) {
    const dir = scratchDir("prr-exec-");
    dirs.push(dir);
    const path = join(dir, "claude-execution-output.json");
    writeFileSync(path, JSON.stringify(events));
    return path;
  }

  it("appends a compact cost + PRs/week footer from the execution log", () => {
    const executionFile = writeExecLog([
      { type: "system", subtype: "init", model: "claude-sonnet-5" },
      { type: "result", subtype: "success", total_cost_usd: 0.16 },
    ]);
    const { payload, summary } = run(
      { summary: "looks good", findings: [] },
      { executionFile, maxWeekly: "2000" },
    );
    assert.match(payload.body, /^looks good\n\n---\n/);
    assert.match(
      payload.body,
      /📊 Review cost: \*\*\$0\.16\*\* \(claude-sonnet-5\)\./,
    );
    // 2000 / 0.16 = 12,500 PRs/week.
    assert.match(
      payload.body,
      /📉 ~12,500 PRs\/week at this rate on a Max 20× plan\./,
    );
    // No hidden cost marker: nothing reads the cost back out of the body.
    assert.doesNotMatch(payload.body, /<!-- review-cost/);
    // The fallback summary file carries the identical footered body.
    assert.equal(summary, payload.body);
  });

  it("computes PRs/week from cost and the weekly budget", () => {
    const executionFile = writeExecLog([
      { type: "result", total_cost_usd: 10 },
    ]);
    const { payload } = run(
      { summary: "s", findings: [] },
      { executionFile, maxWeekly: "1000" },
    );
    assert.match(payload.body, /📊 Review cost: \*\*\$10\.00\*\*\./);
    // floor(1000 / 10) = 100 PRs/week.
    assert.match(payload.body, /~100 PRs\/week at this rate/);
  });

  it("surfaces a runaway cost as ~0 PRs/week", () => {
    // A cost above the weekly budget: floor(1000 / 2469) = 0.
    const executionFile = writeExecLog([
      { type: "result", total_cost_usd: 2469 },
    ]);
    const { payload } = run(
      { summary: "s", findings: [] },
      { executionFile, maxWeekly: "1000" },
    );
    assert.match(payload.body, /~0 PRs\/week at this rate/);
  });

  it("renders sub-cent costs with four decimals", () => {
    const executionFile = writeExecLog([
      { type: "result", total_cost_usd: 0.0009 },
    ]);
    const { payload } = run(
      { summary: "s", findings: [] },
      { executionFile, maxWeekly: "2000" },
    );
    assert.match(payload.body, /📊 Review cost: \*\*\$0\.0009\*\*/);
  });

  it("uses the footer as the body when there is no summary but a comment exists", () => {
    const executionFile = writeExecLog([{ type: "result", total_cost_usd: 1 }]);
    const { payload } = run(
      {
        summary: "",
        findings: [
          {
            path: "src/foo.js",
            line: 2,
            side: "RIGHT",
            severity: "warning",
            title: "t",
            body: "b",
          },
        ],
      },
      { executionFile },
    );
    assert.equal(payload.comments.length, 1);
    // Not the "Automated review." placeholder — the footer stands in as the body.
    assert.match(payload.body, /📊 Review cost:/);
    assert.doesNotMatch(payload.body, /Automated review\./);
  });

  it("omits the footer when the execution log is missing", () => {
    const { payload } = run(
      { summary: "looks good", findings: [] },
      { executionFile: "/nonexistent/claude-execution-output.json" },
    );
    assert.equal(payload.body, "looks good");
  });

  it("omits the footer when the execution log has no cost", () => {
    const executionFile = writeExecLog([
      { type: "system", subtype: "init", model: "claude-sonnet-5" },
    ]);
    const { payload } = run(
      { summary: "looks good", findings: [] },
      { executionFile },
    );
    assert.equal(payload.body, "looks good");
  });

  it("does not throw on a malformed execution log", () => {
    const dir = scratchDir("prr-exec-");
    dirs.push(dir);
    const executionFile = join(dir, "claude-execution-output.json");
    writeFileSync(executionFile, "{ not json");
    const { status, payload } = run(
      { summary: "looks good", findings: [] },
      { executionFile },
    );
    assert.equal(status, "PAYLOAD");
    assert.equal(payload.body, "looks good");
  });
});

describe("post-pr-review: output sanitization", () => {
  it("strips invisible + ANSI payloads the model echoed into a comment body", () => {
    const { payload } = run({
      summary: "needs changes",
      findings: [
        {
          path: "src/foo.js",
          line: 2,
          side: "RIGHT",
          severity: "warning",
          title: "bug\u200Bhere",
          body: "fix \x1b[31mthis\x1b[0m now",
        },
      ],
    });
    assert.equal(payload.comments.length, 1);
    assert.ok(!payload.comments[0].body.includes("\u200B"));
    assert.ok(!payload.comments[0].body.includes("\x1b"));
    assert.match(payload.comments[0].body, /bughere/);
    assert.match(payload.comments[0].body, /fix this now/);
  });

  it("strips invisible + ANSI payloads from the summary/spill body", () => {
    const { payload } = run({
      summary: "all\u200B good \x1b[1mhere\x1b[0m",
      findings: [],
    });
    assert.ok(!payload.body.includes("\u200B"));
    assert.ok(!payload.body.includes("\x1b"));
    assert.equal(payload.body, "all good here");
  });
});

// The severity model lives in config/review-severities.json. These tests stage a
// COPY of the script beside a config of their own, because the script resolves
// that file relative to its own location — the property that keeps an untrusted
// PR head from supplying the config that decides which of its findings hold the
// merge. Editing the real config in place would race every other test.
describe("post-pr-review: the severity config is the single source of truth", () => {
  const SCRIPTS = __dirname;

  // Stage <root>/.github/scripts/{post-pr-review,lib-review-cost}.mjs beside
  // <root>/config/review-severities.json, with node_modules symlinked so the
  // sanitizer import still resolves. Returns the staged script's path.
  function stage(configText) {
    const root = scratchDir("prr-ssot-");
    dirs.push(root);
    const scripts = join(root, ".github", "scripts");
    mkdirSync(scripts, { recursive: true });
    mkdirSync(join(root, "config"), { recursive: true });
    for (const f of ["post-pr-review.mjs", "lib-review-cost.mjs"])
      copyFileSync(join(SCRIPTS, f), join(scripts, f));
    symlinkSync(join(SCRIPTS, "node_modules"), join(scripts, "node_modules"));
    if (configText !== null)
      writeFileSync(join(root, "config", "review-severities.json"), configText);
    return join(scripts, "post-pr-review.mjs");
  }

  // Same contract as run() above, against a staged script + config.
  function runStaged(configText, review) {
    const script = stage(configText);
    const dir = scratchDir("prr-in-");
    dirs.push(dir);
    writeFileSync(join(dir, "diff.txt"), DIFF);
    writeFileSync(join(dir, "review.json"), JSON.stringify(review));
    const env = { ...process.env, PR_INPUT_DIR: dir, EXECUTION_FILE: "" };
    delete env.RUNNER_TEMP;
    const res = spawnSync("node", [script], { env, encoding: "utf8" });
    const payloadPath = join(dir, "review-payload.json");
    return {
      code: res.status,
      stderr: res.stderr,
      payload: existsSync(payloadPath)
        ? JSON.parse(readFileSync(payloadPath, "utf8"))
        : null,
    };
  }

  const NIT = {
    verdict: "looks_good",
    summary: "one small thing",
    findings: [
      {
        path: "src/foo.js",
        line: 2,
        side: "RIGHT",
        severity: "nit",
        title: "naming",
        body: "prefer b2",
      },
    ],
  };
  const config = (over) =>
    JSON.stringify({
      gating: ["blocking", "warning", "nit"],
      icons: { blocking: "🔴", warning: "🟡", nit: "🔵" },
      ...over,
    });

  // An un-anchorable nit: a gating severity takes the synthetic anchor, a
  // non-gating one spills, so the `gating` list decides which happens.
  const UNANCHORABLE_NIT = {
    summary: "one small thing",
    findings: [
      {
        path: "src/foo.js",
        line: 999,
        side: "RIGHT",
        severity: "nit",
        title: "naming",
        body: "prefer b2",
      },
    ],
  };

  it("drops a severity from `gating` and the finding stops opening a thread", () => {
    // THE falsifier for the whole wiring: identical input, one edited config
    // value, and a finding that opened a resolvable thread now only spills. If
    // both runs agreed, the config would be documentation nobody reads.
    const held = runStaged(config({}), UNANCHORABLE_NIT);
    assert.equal(held.payload.comments.length, 1);
    assert.match(held.payload.comments[0].body, /PR-wide finding at/);
    const released = runStaged(
      config({ gating: ["blocking", "warning"] }),
      UNANCHORABLE_NIT,
    );
    assert.deepEqual(released.payload.comments, []);
    assert.match(released.payload.body, /#### Additional notes/);
  });

  it("an edited icon reaches the posted comment body", () => {
    const { payload } = runStaged(
      config({ icons: { blocking: "🔴", warning: "🟡", nit: "🧢" } }),
      NIT,
    );
    assert.match(payload.comments[0].body, /^🧢 /);
  });

  it("renders the icon and marker for a severity the model cased differently", () => {
    // Membership is tested lowercased, so a cased severity gates; the glyph and
    // the marker must follow it rather than falling back to "•" and no marker.
    const { payload } = runStaged(config({}), {
      ...NIT,
      findings: [{ ...NIT.findings[0], severity: " Nit " }],
    });
    assert.equal(
      payload.comments[0].body,
      "🔵 naming — prefer b2\n\n<!-- severity: nit -->",
    );
  });

  it("an absent config keeps the shipped model rather than failing", () => {
    // `config/` is not in template-sync's SYNC_PATHS, so every repo that syncs
    // this script gets it WITHOUT the config file. Treating that as fatal would
    // red every review in every downstream repo, on a file that cannot arrive.
    // The shipped model gates blocking and warning, so an un-anchorable nit
    // spills rather than taking a synthetic anchor.
    const { code, payload } = runStaged(null, UNANCHORABLE_NIT);
    assert.equal(code, 0);
    assert.deepEqual(payload.comments, []);
    assert.match(payload.body, /#### Additional notes/);
  });

  for (const [why, text] of [
    ["the config is not JSON", "{ not json"],
    // An empty gating set lets every finding spill into the review body, where
    // nothing holds the merge — the one failure a default would make invisible.
    ["`gating` is empty", config({ gating: [] })],
    ["`gating` is absent", '{"icons":{"nit":"🔵"}}'],
    ["`icons` is absent", '{"gating":["nit"]}'],
    ["the config is a bare JSON null", "null"],
    ["`icons` is an array", '{"gating":["0"],"icons":["🔵"]}'],
    ["a gating severity has no icon", config({ icons: { blocking: "🔴" } })],
    ["an icon is not a string", config({ icons: { nit: 7 } })],
  ]) {
    it(`fails closed when ${why}`, () => {
      const { code, payload, stderr } = runStaged(text, NIT);
      assert.notEqual(code, 0, stderr);
      assert.equal(
        payload,
        null,
        "nothing may post on an unknown severity model",
      );
      assert.match(stderr, /review-severities\.json/);
    });
  }
});
