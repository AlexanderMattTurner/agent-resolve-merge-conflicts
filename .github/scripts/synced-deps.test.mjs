import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { scratchDir } from "./lib-test-scratch.mjs";

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const TEMPLATE_SYNC_YAML = join(
  REPO_ROOT,
  ".github",
  "workflows",
  "template-sync.yaml",
);

// SYNC_PATHS is the whole contract: a consumer receives these paths and nothing
// else. Reading it here rather than restating the list is what makes the tests
// below fail when a synced file's data dependency stops being delivered.
function syncPaths() {
  const yaml = readFileSync(TEMPLATE_SYNC_YAML, "utf8");
  const match = /^\s*SYNC_PATHS:\s*"(?<paths>[^"]*)"/m.exec(yaml);
  assert.ok(match, "template-sync.yaml declares SYNC_PATHS");
  return match.groups.paths.split(/\s+/).filter(Boolean);
}

// The tree a consumer actually gets: every SYNC_PATHS entry copied out of the
// template, and nothing outside them. An installer that reads a pin the sync
// does not deliver dies here exactly as it dies in the consumer's CI.
function consumerTree() {
  const root = scratchDir("consumer-");
  for (const path of syncPaths()) {
    mkdirSync(join(root, dirname(path)), { recursive: true });
    cpSync(join(REPO_ROOT, path), join(root, path), { recursive: true });
  }
  return root;
}

function stubDir(root, stubs) {
  const dir = join(root, "stub");
  mkdirSync(dir, { recursive: true });
  for (const [name, body] of Object.entries(stubs)) {
    writeFileSync(join(dir, name), `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  }
  return dir;
}

function runInstaller(root, script, stubs, args = []) {
  const dir = stubDir(root, stubs);
  return spawnSync("bash", [join(root, ".github", script), ...args], {
    encoding: "utf8",
    env: { ...process.env, PATH: `${dir}:${process.env.PATH}` },
  });
}

test("template-sync delivers the mergiraf pin install-mergiraf reads", () => {
  const root = consumerTree();
  try {
    const run = runInstaller(
      root,
      "scripts/install-mergiraf.sh",
      { curl: 'echo "REACHED-DOWNLOAD" >&2\nexit 42' },
      [join(root, "dest")],
    );
    assert.doesNotMatch(run.stderr, /No such file or directory/);
    assert.match(run.stderr, /REACHED-DOWNLOAD/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

<<<<<<< local
// The sync delivers a default CLI pin; each repository may add its own override,
// which its own Dependabot bumps. A version of null deletes that file.
const DEFAULT_PIN = join(".github", "claude-cli-default", "package.json");
const OVERRIDE_PIN = join(".github", "claude-cli", "package.json");

function writePin(root, path, version) {
  if (version === null) {
    rmSync(join(root, path), { force: true });
    return;
  }
  mkdirSync(join(root, dirname(path)), { recursive: true });
  writeFileSync(
    join(root, path),
    JSON.stringify({ dependencies: { "@anthropic-ai/claude-code": version } }),
  );
}

// `defaultVersion` undefined keeps the default exactly as the sync delivered it.
function runClaudeInstaller({ override, defaultVersion } = {}) {
||||||| base
test("template-sync delivers the CLI pin install-claude-cli reads", () => {
=======
// install-claude-cli reads the repo's own override when it exists, else the
// template's synced default, and refuses naming both paths when neither exists.
const CLI_OVERRIDE = join(".github", "claude-cli", "package.json");
const CLI_DEFAULT = join(".github", "claude-cli-default", "package.json");
const CLI_STUBS = {
  npm: 'echo "REACHED-INSTALL $*" >&2\nexit 0',
  claude: 'echo "2.0.0"',
};

function pinnedVersion(file) {
  return JSON.parse(readFileSync(file, "utf8")).dependencies[
    "@anthropic-ai/claude-code"
  ];
}

function assertInstalls(run, version) {
  assert.equal(run.status, 0, run.stderr);
  assert.ok(
    run.stderr.includes(
      `REACHED-INSTALL install -g @anthropic-ai/claude-code@${version}\n`,
    ),
    run.stderr,
  );
}

test("install-claude-cli prefers the repo's own override", () => {
>>>>>>> template
  const root = consumerTree();
  try {
<<<<<<< local
    if (defaultVersion !== undefined)
      writePin(root, DEFAULT_PIN, defaultVersion);
    if (override !== undefined) writePin(root, OVERRIDE_PIN, override);
    return runInstaller(root, "resolver/install-claude-cli.sh", {
      npm: 'echo "REACHED-INSTALL $*" >&2\nexit 0',
      claude: 'echo "0.0.1"',
    });
||||||| base
    const run = runInstaller(root, "install-claude-cli.sh", {
      npm: 'echo "REACHED-INSTALL $*" >&2\nexit 0',
      claude: 'echo "2.0.0"',
    });
    assert.doesNotMatch(run.stderr, /No such file or directory/);
    assert.match(
      run.stderr,
      /REACHED-INSTALL .*@anthropic-ai\/claude-code@\d+\.\d+\.\d+/,
    );
    assert.equal(run.status, 0);
=======
    mkdirSync(join(root, dirname(CLI_OVERRIDE)), { recursive: true });
    writeFileSync(
      join(root, CLI_OVERRIDE),
      JSON.stringify({
        dependencies: { "@anthropic-ai/claude-code": "9.8.7" },
      }),
    );
    assert.notEqual(pinnedVersion(join(root, CLI_DEFAULT)), "9.8.7");
    assertInstalls(
      runInstaller(root, "install-claude-cli.sh", CLI_STUBS),
      "9.8.7",
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("install-claude-cli falls back to the synced default", () => {
  const root = consumerTree();
  try {
    assert.ok(
      !existsSync(join(root, CLI_OVERRIDE)),
      "the sync delivers no override",
    );
    const run = runInstaller(root, "install-claude-cli.sh", CLI_STUBS);
    assertInstalls(run, pinnedVersion(join(root, CLI_DEFAULT)));
>>>>>>> template
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

test("install-claude-cli installs the repository's override over the default", () => {
  const run = runClaudeInstaller({
    override: "9.8.7",
    defaultVersion: "1.2.3",
  });
  assert.match(
    run.stderr,
    /REACHED-INSTALL .*@anthropic-ai\/claude-code@9\.8\.7\b/,
  );
  assert.equal(run.status, 0);
});

test("install-claude-cli installs the synced default when the repository has no override", () => {
  const delivered = JSON.parse(
    readFileSync(join(REPO_ROOT, DEFAULT_PIN), "utf8"),
  ).dependencies["@anthropic-ai/claude-code"];
  const run = runClaudeInstaller();
  assert.ok(
    run.stderr.includes(
      `REACHED-INSTALL install -g @anthropic-ai/claude-code@${delivered}`,
    ),
    run.stderr,
  );
  assert.equal(run.status, 0);
});

test("install-claude-cli refuses, naming both pins, when neither exists", () => {
  const run = runClaudeInstaller({ defaultVersion: null });
  assert.match(run.stderr, /claude-cli\/package\.json/);
  assert.match(run.stderr, /claude-cli-default\/package\.json/);
  assert.doesNotMatch(run.stderr, /REACHED-INSTALL/);
  assert.equal(run.status, 1);
});

test("install-claude-cli refuses a pin that is not an exact version", () => {
  const run = runClaudeInstaller({ override: "^2.1.0" });
  assert.match(run.stderr, /exact X\.Y\.Z version/);
  assert.doesNotMatch(run.stderr, /REACHED-INSTALL/);
  assert.equal(run.status, 1);
});

// Dependabot rewrites these pins unattended: a range or a malformed file must
// turn CI red here, before the installer meets it on a runner.
test("every Claude CLI pin in the repo is an exact version", () => {
  const present = [OVERRIDE_PIN, DEFAULT_PIN].filter((path) =>
    existsSync(join(REPO_ROOT, path)),
  );
  assert.ok(present.length > 0, "neither Claude CLI pin file exists");
  for (const path of present) {
    const pinned = JSON.parse(readFileSync(join(REPO_ROOT, path), "utf8"))
      .dependencies["@anthropic-ai/claude-code"];
    assert.match(
      pinned,
      /^\d+\.\d+\.\d+$/,
      `${path} must pin @anthropic-ai/claude-code to one exact version`,
    );
  }
});

test("install-claude-cli refuses naming both pins when neither exists", () => {
  const root = consumerTree();
  try {
    rmSync(join(root, dirname(CLI_DEFAULT)), { recursive: true, force: true });
    const run = runInstaller(root, "install-claude-cli.sh", CLI_STUBS);
    assert.equal(run.status, 1);
    assert.match(run.stderr, /claude-cli\/package\.json/);
    assert.match(run.stderr, /claude-cli-default\/package\.json/);
    assert.doesNotMatch(run.stderr, /REACHED-INSTALL/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("install-claude-cli refuses a range instead of an exact version", () => {
  const root = consumerTree();
  try {
    mkdirSync(join(root, dirname(CLI_OVERRIDE)), { recursive: true });
    writeFileSync(
      join(root, CLI_OVERRIDE),
      JSON.stringify({
        dependencies: { "@anthropic-ai/claude-code": "^9.8.7" },
      }),
    );
    const run = runInstaller(root, "install-claude-cli.sh", CLI_STUBS);
    assert.equal(run.status, 1);
    assert.match(run.stderr, /claude-cli\/package\.json/);
    assert.doesNotMatch(run.stderr, /REACHED-INSTALL/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

// Dependabot rewrites these pins unattended: a range or a malformed file must
// turn CI red here, before the installer meets it on a runner.
test("every Claude CLI pin in the repo is an exact version", () => {
  const present = [CLI_OVERRIDE, CLI_DEFAULT].filter((path) =>
    existsSync(join(REPO_ROOT, path)),
  );
  assert.ok(present.length > 0, "neither Claude CLI pin file exists");
  for (const path of present) {
    assert.match(
      pinnedVersion(join(REPO_ROOT, path)),
      /^\d+\.\d+\.\d+$/,
      `${path} must pin @anthropic-ai/claude-code to one exact version`,
    );
  }
});

// The commit-msg hook passes this path to commitlint with no fallback, and the
// hook runs under `set -euo pipefail`. Undelivered, every commit in the consumer
// fails with a commitlint ENOENT rather than a message about the hook.
test("template-sync delivers the commitlint config the commit-msg hook names", () => {
  const hook = readFileSync(join(REPO_ROOT, ".hooks", "commit-msg"), "utf8");
  const declared = /^config="(?<path>[^"]+)"$/m.exec(hook);
  assert.ok(declared, ".hooks/commit-msg declares its commitlint config path");

  const root = consumerTree();
  try {
    assert.ok(
      existsSync(join(root, declared.groups.path)),
      `${declared.groups.path} is read by .hooks/commit-msg but no SYNC_PATHS entry delivers it`,
    );
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
