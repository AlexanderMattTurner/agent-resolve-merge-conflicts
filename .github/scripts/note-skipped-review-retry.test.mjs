// note-skipped-review.sh posts the review that clears the review-findings gate's
// first leg for a PR the reviewer skips, so what matters is that the review's text
// reaches the PR even when the reviews API refuses the call. Drives the real script
// (which posts through lib-post-review-with-retry.sh) against a
// fake `gh` that rejects a chosen set of events, so the fallback path is exercised
// end to end rather than re-implemented.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { writeFileSync, chmodSync, rmSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { scratchDir } from "./lib-test-scratch.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(HERE, "..", "..");
const SCRIPT = join(HERE, "note-skipped-review.sh");

// A fake `gh` that rejects (422) any reviews-API POST whose payload `event`
// is in `rejectEvents`, and always accepts `gh pr comment` (the last-resort
// fallback). `gh api -X POST … --input FILE` always lands FILE at $6, since
// the shared helper's call shape is fixed.
function run({ rejectEvents = [] } = {}) {
  const bin = scratchDir("note-skipped-bin-");

  const reject = rejectEvents.join(" ");
  const ghPath = join(bin, "gh");
  writeFileSync(
    ghPath,
    "#!/usr/bin/env bash\n" +
      `REJECT="${reject}"\n` +
      'if [[ "$1" == "api" ]]; then\n' +
      '  file="$6"\n' +
      '  event="$(node -e \'console.log(JSON.parse(require("fs").readFileSync(process.argv[1])).event)\' "$file")"\n' +
      "  for e in $REJECT; do\n" +
      '    if [[ "$e" == "$event" ]]; then\n' +
      '      echo "gh: Unprocessable Entity (HTTP 422)" >&2\n' +
      "      exit 1\n" +
      "    fi\n" +
      "  done\n" +
      '  echo "posted event=$event" >&2\n' +
      "  exit 0\n" +
      'elif [[ "$1" == "pr" && "$2" == "comment" ]]; then\n' +
      '  echo "posted fallback comment" >&2\n' +
      "  exit 0\n" +
      "fi\n" +
      'echo "fake gh: unexpected invocation: $*" >&2\n' +
      "exit 1\n",
  );
  chmodSync(ghPath, 0o755);

  const res = spawnSync("bash", [SCRIPT], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: {
      ...process.env,
      PATH: `${bin}:${process.env.PATH ?? ""}`,
      PR: "1",
      GH_REPO: "owner/repo",
    },
  });
  rmSync(bin, { recursive: true, force: true });
  return res;
}

test("the note posts as a COMMENT review, never a vote", () => {
  const res = run();
  assert.equal(res.status, 0, res.stderr);
  assert.match(res.stderr, /posted event=COMMENT/);
  assert.doesNotMatch(res.stderr, /posted event=APPROVE/);
  assert.doesNotMatch(res.stderr, /posting a summary comment instead/);
});

test("a rejected review still delivers its text as a plain comment", () => {
  const res = run({ rejectEvents: ["COMMENT"] });
  assert.equal(res.status, 0, res.stderr);
  assert.match(res.stderr, /posting a summary comment instead/);
  assert.match(res.stderr, /posted fallback comment/);
});
