// auto-approve-skipped-pr.sh's idempotency guard has to answer "is this PR
// approved" the way the RULESET answers it: under `dismiss_stale_reviews` an
// approval counts only on the commit it was cast against. The reviews API keeps
// reporting a superseded one as APPROVED, so a guard that reads the state alone
// skips after every push and strands the PR with no approval that counts.
//
// Drives the real script against a fake `gh` holding a chosen review list and
// head sha, and asserts on whether `gh pr review --approve` was invoked.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { writeFileSync, chmodSync, readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { scratchDir } from "./lib-test-scratch.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(HERE, "..", "..");
const SCRIPT = join(HERE, "auto-approve-skipped-pr.sh");

// `reviews` is the [state, commit_id] list the fake reviews endpoint holds. The
// fake serves real JSON and runs the REAL `jq` with the script's own `--jq`
// expression, so the filter under test is exercised rather than assumed.
function run({ head, reviews }) {
  const bin = scratchDir("auto-approve-head-bin-");
  const log = join(bin, "approve.log");
  const prJson = join(bin, "pr.json");
  const reviewsJson = join(bin, "reviews.json");
  writeFileSync(prJson, JSON.stringify({ head: { sha: head } }));
  writeFileSync(
    reviewsJson,
    JSON.stringify(
      reviews.map(([state, sha]) => ({
        user: { login: "github-actions[bot]" },
        state,
        commit_id: sha,
      })),
    ),
  );
  // These are bash lines, so `${…}` is parameter expansion, not a JS template.
  /* eslint-disable no-template-curly-in-string */
  writeFileSync(
    join(bin, "gh"),
    [
      "#!/usr/bin/env bash",
      "set -euo pipefail",
      'if [[ "$1" == "pr" && "$2" == "review" ]]; then',
      `  echo "$*" >>"${log}"`,
      "  exit 0",
      "fi",
      '[[ "$1" == "api" ]] || { echo "fake gh: unexpected: $*" >&2; exit 1; }',
      "# The endpoint decides which fixture; --jq's value is the next argument.",
      "src=",
      "filter=",
      "for ((i = 1; i <= $#; i++)); do",
      '  case "${!i}" in',
      `  */pulls/*/reviews) src="${reviewsJson}" ;;`,
      `  */pulls/*) src="\${src:-${prJson}}" ;;`,
      '  --jq) j=$((i + 1)); filter="${!j}" ;;',
      "  esac",
      "done",
      '[[ -n "$src" && -n "$filter" ]] || { echo "fake gh: unmatched: $*" >&2; exit 1; }',
      "# --paginate streams each array element, which is what `.[] | …` expects.",
      'jq -r "$filter" "$src"',
      "",
    ].join("\n"),
  );
  /* eslint-enable no-template-curly-in-string */
  chmodSync(join(bin, "gh"), 0o755);

  const res = spawnSync("bash", [SCRIPT], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: {
      ...process.env,
      PATH: `${bin}:${process.env.PATH ?? ""}`,
      PR: "1",
      GH_REPO: "owner/repo",
      GH_TOKEN: "x",
    },
  });
  return {
    status: res.status,
    stdout: res.stdout,
    stderr: res.stderr,
    approved: existsSync(log) ? readFileSync(log, "utf8") : "",
  };
}

const HEAD = "b577991b97cbc9142a3c6f00d36e487d4077a62e";
const OLD = "df5d3c66590bcf55add024f20e339c2666f0bf2a";

test("a PR with no github-actions review is approved", () => {
  const r = run({ head: HEAD, reviews: [] });
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.approved, /--approve/);
});

test("an approval on the CURRENT head is not posted twice", () => {
  const r = run({ head: HEAD, reviews: [["APPROVED", HEAD]] });
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.approved, "");
  assert.match(
    r.stdout,
    new RegExp(`already carries a github-actions approval on ${HEAD}`),
  );
});

test("an approval left on a SUPERSEDED head is re-posted", () => {
  const r = run({ head: HEAD, reviews: [["APPROVED", OLD]] });
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.approved, /--approve/);
});

test("a dismissal is honoured at any sha", () => {
  const r = run({ head: HEAD, reviews: [["DISMISSED", OLD]] });
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.approved, "");
  assert.match(r.stdout, /carries a dismissed github-actions review/);
});
