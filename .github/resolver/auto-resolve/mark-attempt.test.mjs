// mark-attempt.sh — the write half of the one-attempt-per-head rule.
// discover reads the mark (tests/test_auto_resolve_discover.py covers that
// side); this covers what gets written, because a mark on the wrong SHA is
// indistinguishable from no mark at all until a PR silently resolves twice.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { writeFileSync, readFileSync, chmodSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { scratchDir } from "../../scripts/lib-test-scratch.mjs";
import { git } from "../../scripts/lib-test-git.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const SCRIPT = join(HERE, "mark-attempt.sh");

// A one-commit repo plus a recording `gh`; returns the run result, the recorded
// gh argv lines, and the SHA the script should have marked.
// A `gh` that answers by the QUESTION each call asks, read off its own `--jq`
// filter, so a test can pose a head another run holds without depending on the
// order the script happens to ask its questions in.
const GH_STUB = `#!/usr/bin/env bash
printf '%s\\n' "$*" >> "%LOG%"
if [[ "$*" == *"--method POST"* ]]; then echo 7; exit 0; fi
if [[ "$*" == *"/actions/runs/"* ]]; then echo "\${RUN_STATUS:-}"; exit \${RUN_READ_EXIT:-0}; fi
if [[ "$*" == *"per_page=100"* ]]; then echo "\${BOUGHT_COUNT:-0}"; exit 0; fi
if [[ "$*" == *'$marked > (now -'* ]]; then echo "\${MARK_FRESH:-false}"; exit 0; fi
if [[ "$*" == *"min_by(.id)"* ]]; then echo "\${CLAIM_AGE:-30} \${CLAIM_URL:-}"; exit 0; fi
if [[ "$*" == *'.id] | min // 0'* ]]; then echo "\${CLAIM_ID:-0}"; exit 0; fi
echo 0
exit 0
`;

function runMark({ ghExit = 0, withOutputFile = true, gh = {} } = {}) {
  const root = scratchDir("auto-resolve-mark-");
  const work = join(root, "work");
  git(root, "init", "-q", work);
  git(work, "config", "user.email", "t@t");
  git(work, "config", "user.name", "t");
  git(work, "config", "commit.gpgsign", "false");
  writeFileSync(join(work, "a.md"), "a\n");
  git(work, "add", "-A");
  git(work, "commit", "-q", "-m", "base");

  const ghLog = join(root, "gh-calls");
  writeFileSync(ghLog, "");
  const ghPath = join(root, "gh");
  writeFileSync(
    ghPath,
    ghExit === 0
      ? GH_STUB.replace("%LOG%", ghLog)
      : `#!/usr/bin/env bash\nprintf '%s\\n' "$*" >> "${ghLog}"\nexit ${ghExit}\n`,
  );
  chmodSync(ghPath, 0o755);

  const outputFile = join(root, "github-output");
  if (withOutputFile) writeFileSync(outputFile, "");

  const res = spawnSync("bash", [SCRIPT], {
    cwd: work,
    encoding: "utf8",
    env: {
      ...process.env,
      PATH: `${root}:${process.env.PATH ?? ""}`,
      REPO: "owner/repo",
      GH_TOKEN: "x",
      // Always overridden: inheriting CI's own GITHUB_OUTPUT would have the
      // no-output case append to the real runner's file.
      GITHUB_OUTPUT: withOutputFile ? outputFile : "",
      // One attempt, no backoff: the gh-down case would otherwise sit through the
      // retry ladder.
      RETRY_MAX: "1",
      RETRY_BASE_DELAY: "0",
      GITHUB_SERVER_URL: "https://github.com",
      GITHUB_REPOSITORY: "owner/repo",
      GITHUB_RUN_ID: "999",
      ...gh,
    },
  });
  return {
    res,
    ghCalls: readFileSync(ghLog, "utf8").split("\n").filter(Boolean),
    sha: git(work, "rev-parse", "HEAD").trim(),
    outputs: withOutputFile ? readFileSync(outputFile, "utf8") : "",
  };
}

test("it marks the checked-out head commit with the context discover reads", () => {
  const { res, ghCalls, sha } = runMark();
  assert.equal(res.status, 0, res.stderr);
  const post = ghCalls.find((c) => c.includes("--method POST"));
  assert.ok(post, ghCalls.join("\n"));
  // The SHA is the tree this run resolves, not whatever discover saw earlier.
  assert.ok(post.includes(`repos/owner/repo/statuses/${sha}`), post);
  // The context string is the contract with discover's filter; a typo here is a
  // mark nothing ever reads.
  assert.ok(post.includes("context=auto-resolve/attempted"), post);
  assert.ok(post.includes("state=success"), post);
});

test("it publishes the SHA it marked, so a later step can release that mark", () => {
  const { res, outputs, sha } = runMark();
  assert.equal(res.status, 0, res.stderr);
  // The exact SHA, not a prefix: release-attempt.sh posts a status on whatever
  // this says, and a status on a commit-ish nobody marked releases nothing.
  assert.ok(outputs.includes(`head_sha=${sha}`), outputs);
  // The claim is what the outcome gate reads: a run that owns the head and then
  // lands nothing is a stall, while one that stood down on a live run is not.
  assert.ok(outputs.includes("claim=owned"), outputs);
});

test("it marks the head with the run that holds the claim", () => {
  // Without the run on the mark, a later run cannot tell a claim someone is
  // working from one whose run died before it released anything.
  const { res, ghCalls } = runMark();
  assert.equal(res.status, 0, res.stderr);
  const post = ghCalls.find((c) => c.includes("--method POST"));
  assert.ok(
    post.includes("target_url=https://github.com/owner/repo/actions/runs/999"),
    post,
  );
});

test("a head another run is still working stands this run down", () => {
  const { res, ghCalls, outputs } = runMark({
    gh: {
      MARK_FRESH: "true",
      CLAIM_URL: "https://github.com/owner/repo/actions/runs/321",
      RUN_STATUS: "in_progress",
    },
  });
  assert.equal(res.status, 0, res.stderr);
  assert.ok(outputs.includes("claim=duplicate"), outputs);
  assert.ok(outputs.includes("already_claimed=true"), outputs);
  // Nothing was written: the other run owns the head, so a second mark here
  // would race its claim.
  assert.equal(
    ghCalls.filter((c) => c.includes("--method POST")).length,
    0,
    ghCalls.join("\n"),
  );
});

test("a mark held by a concluded run that bought nothing is released and taken over", () => {
  const { res, ghCalls, outputs, sha } = runMark({
    gh: {
      MARK_FRESH: "true",
      CLAIM_URL: "https://github.com/owner/repo/actions/runs/321",
      RUN_STATUS: "completed",
      BOUGHT_COUNT: "0",
    },
  });
  assert.equal(res.status, 0, res.stderr);
  // The stale mark is cancelled first, then this run claims the head. Without
  // the release the head stays latched for the whole TTL and every later run
  // stands down on a claim nobody is working.
  const posts = ghCalls.filter((c) => c.includes("--method POST"));
  assert.ok(
    posts[0].includes("context=auto-resolve/attempted-released"),
    posts.join("\n"),
  );
  assert.ok(
    posts[1].includes("context=auto-resolve/attempted"),
    posts.join("\n"),
  );
  assert.ok(outputs.includes(`head_sha=${sha}`), outputs);
  assert.ok(outputs.includes("claim=owned"), outputs);
});

test("a takeover keeps the claim even when an older mark still holds the head", () => {
  // The release is what makes the takeover work. Without it the dead run's mark
  // is still the oldest unreleased one, so the arbitration below hands the claim
  // straight back and this run stands down as a green `duplicate` — the ending
  // this change exists to remove. CLAIM_ID is that older mark's id, below the 7
  // this stub gives the new one.
  const { res, ghCalls, outputs, sha } = runMark({
    gh: {
      MARK_FRESH: "true",
      CLAIM_URL: "https://github.com/owner/repo/actions/runs/321",
      RUN_STATUS: "completed",
      CLAIM_ID: "3",
    },
  });
  assert.equal(res.status, 0, res.stderr);
  assert.ok(outputs.includes("claim=owned"), outputs);
  assert.ok(outputs.includes(`head_sha=${sha}`), outputs);
  assert.ok(!outputs.includes("claim=duplicate"), outputs);
  assert.ok(
    ghCalls.some(
      (c) => c.includes("--method POST") && c.includes("attempted-released"),
    ),
    ghCalls.join("\n"),
  );
});

test("an older mark this run did not take over still wins the claim", () => {
  // The arbitration a takeover skips is still live on the ordinary path: two runs
  // that both raced past an unmarked head settle on the lower mark id.
  const { res, outputs } = runMark({ gh: { CLAIM_ID: "3" } });
  assert.equal(res.status, 0, res.stderr);
  assert.ok(outputs.includes("claim=duplicate"), outputs);
  assert.ok(!outputs.includes("claim=owned"), outputs);
});

test("a mark naming no run is taken over once it outlives any run", () => {
  const { res, ghCalls, outputs } = runMark({
    gh: { MARK_FRESH: "true", CLAIM_URL: "", CLAIM_AGE: "9000" },
  });
  assert.equal(res.status, 0, res.stderr);
  assert.ok(outputs.includes("claim=owned"), outputs);
  assert.ok(
    ghCalls.some(
      (c) => c.includes("--method POST") && c.includes("attempted-released"),
    ),
    ghCalls.join("\n"),
  );
});

test("a run-status read that fails stands the run down rather than taking over", () => {
  // Fail-closed on money: an unreadable holder may be spending right now.
  const { res, ghCalls, outputs } = runMark({
    gh: {
      MARK_FRESH: "true",
      CLAIM_URL: "https://github.com/owner/repo/actions/runs/321",
      RUN_READ_EXIT: "1",
    },
  });
  assert.equal(res.status, 0, res.stderr);
  assert.ok(outputs.includes("claim=latched"), outputs);
  assert.equal(
    ghCalls.filter((c) => c.includes("--method POST")).length,
    0,
    ghCalls.join("\n"),
  );
});

test("a mark naming no run is left alone while it is younger than a run's own life", () => {
  // Age is the only liveness evidence such a mark carries, so inside that window
  // it reads as a run still working and this one stands down having spent nothing.
  const { res, outputs } = runMark({
    gh: { MARK_FRESH: "true", CLAIM_URL: "", CLAIM_AGE: "60" },
  });
  assert.equal(res.status, 0, res.stderr);
  assert.ok(outputs.includes("claim=duplicate"), outputs);
  assert.ok(outputs.includes("already_claimed=true"), outputs);
});

test("it marks without an output file, for a caller that is not a runner", () => {
  const { res, ghCalls } = runMark({ withOutputFile: false });
  assert.equal(res.status, 0, res.stderr);
  assert.ok(
    ghCalls.some((c) => c.includes("--method POST")),
    ghCalls.join("\n"),
  );
});

test("a head it could not mark fails the step instead of spending", () => {
  // Fail closed: an unmarked head is one every later scan selects again, so
  // proceeding here spends the model's full price once per scan with nothing to
  // stop it. The old best-effort write printed "Marked ..." either way, which is
  // what made that loop invisible in the log.
  const { res, outputs } = runMark({ ghExit: 1 });
  assert.notEqual(res.status, 0);
  assert.match(
    res.stdout,
    /refusing to spend on a head no later scan would skip/,
  );
  // No head_sha either: a release step posting against a SHA this run failed to
  // mark would release a mark that does not exist.
  assert.equal(outputs.trim(), "");
});
