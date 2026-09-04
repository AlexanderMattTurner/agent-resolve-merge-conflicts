// handoff.sh — the step that gives an unmergeable conflict (a binary,
// or a `-merge` path no resolve-generated rule owns) back to a human BEFORE any
// LLM cost. Two obligations: say what is wrong on the PR, and make sure the next
// base push does not re-run the whole resolver into the same verdict.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { writeFileSync, readFileSync, chmodSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { recordGhCall, statusComments } from "./_gh-shim.mjs";
import { scratchDir } from "../../scripts/lib-test-scratch.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const SCRIPT = join(HERE, "handoff.sh");

// Run handoff with a fake `gh` on PATH that records every invocation, and return
// the recorded argv lines plus the script's exit status and stderr.
function runHandoff(env = {}) {
  const root = scratchDir("auto-resolve-handoff-");
  const ghLog = join(root, "gh-calls");
  writeFileSync(ghLog, "");
  const ghPath = join(root, "gh");
  writeFileSync(ghPath, `#!/usr/bin/env bash\n${recordGhCall(ghLog)}exit 0\n`);
  chmodSync(ghPath, 0o755);
  const res = spawnSync("bash", [SCRIPT], {
    encoding: "utf8",
    env: {
      ...process.env,
      PATH: `${root}:${process.env.PATH ?? ""}`,
      PR: "42",
      BASE_REF: "main",
      GH_REPO: "owner/repo",
      UNRESOLVABLE: "pnpm-lock.yaml",
      ...env,
    },
  });
  const ghCalls = readFileSync(ghLog, "utf8").split("\n").filter(Boolean);
  return {
    status: res.status,
    // The step's diagnosis is a `::error::` workflow command on stdout.
    output: res.stdout + res.stderr,
    ghCalls,
    // What the PR is told: the status comment this run posts or rewrites.
    comments: statusComments(ghCalls),
  };
}

test("an unmergeable conflict fails loud and names the paths on the PR", () => {
  const { status, output, comments } = runHandoff();
  assert.notEqual(status, 0);
  assert.match(output, /unmergeable conflict\(s\) with main: pnpm-lock\.yaml/);
  assert.equal(comments.length, 1);
  assert.ok(comments[0].includes("pnpm-lock.yaml"), comments[0]);
});

test("it blocks later auto-resolve runs instead of re-spending on the same verdict", () => {
  const { ghCalls, comments } = runHandoff();
  // discover skips any PR carrying this label, which is the only thing that stops
  // every push to the base branch re-running a paid resolve into this refusal.
  assert.ok(
    ghCalls.some((c) => c === "pr edit 42 --add-label auto-resolve-blocked"),
    ghCalls.join("\n"),
  );
  // The label has to exist before it can be applied; --force keeps a re-run idempotent.
  assert.ok(
    ghCalls.some((c) => c.startsWith("label create auto-resolve-blocked")),
    ghCalls.join("\n"),
  );
  // And the human is told how to undo it, or the label is a silent off switch.
  assert.ok(comments[0].includes("Remove the label"), comments[0]);
});

test("the comment names the branch whose .gitattributes retires the verdict", () => {
  // The classifier reads the merge attribute from HEAD, because that is the copy
  // `git merge` consulted. So the file a human must change to retire this is the
  // PR branch's own, and a comment naming the BASE sends them to edit a file
  // that has no effect on the answer. It is not a permanent verdict either — the
  // old "nothing about a later push changes this" claim was the #4083 bug.
  const { comments } = runHandoff();
  assert.match(comments[0], /\.gitattributes/);
  assert.match(comments[0], /this branch's own/);
  assert.doesNotMatch(comments[0], /nothing about a later push/);
  // BASE_REF is `main` in runHandoff, and it legitimately appears in the "cannot
  // auto-resolve the merge conflict with `main`" opener — so the assertion is
  // that no sentence sends the reader to the base's copy of the file.
  assert.doesNotMatch(comments[0], /`main`'s current `\.gitattributes`/);
});

test("a failure to label does not swallow the handoff's own error", () => {
  // gh down (every subcommand exits 1): the run must still fail loud with the
  // real diagnosis rather than dying on the best-effort label call.
  const root = scratchDir("auto-resolve-handoff-gh-down-");
  const ghPath = join(root, "gh");
  writeFileSync(ghPath, "#!/usr/bin/env bash\nexit 1\n");
  chmodSync(ghPath, 0o755);
  const res = spawnSync("bash", [SCRIPT], {
    encoding: "utf8",
    env: {
      ...process.env,
      PATH: `${root}:${process.env.PATH ?? ""}`,
      PR: "42",
      BASE_REF: "main",
      GH_REPO: "owner/repo",
      UNRESOLVABLE: "assets/logo.png",
      // One attempt per gh call: the retry wrapper would otherwise back off
      // through its full ladder on every failing invocation.
      RETRY_MAX: "1",
      RETRY_BASE_DELAY: "0",
    },
  });
  assert.notEqual(res.status, 0);
  assert.match(
    res.stdout,
    /unmergeable conflict\(s\) with main: assets\/logo\.png/,
  );
});
