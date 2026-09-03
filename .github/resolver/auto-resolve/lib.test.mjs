import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const LIB = join(HERE, "lib.sh");

// The protected set is the one definition BOTH the prepare log and the land
// step's pushed-resolution warning read, so it is tested where it lives rather
// than through either caller.
function protectedMatches(paths, env = {}) {
  const out = execFileSync(
    "bash",
    ["-c", `source "${LIB}"; protected_matches "$@"`, "_", ...paths],
    { encoding: "utf8", env: { ...process.env, ...env } },
  );
  return out.split("\n").filter(Boolean);
}

// The set a shard may Edit beside its conflicts: the head's own diff, minus
// each class the header of writable_paths names. Driven through a real repo so
// every subtraction is exercised on a real diff, one member per class.
function writablePaths(repo, base, head) {
  const out = execFileSync(
    "bash",
    [
      "-c",
      `source "${LIB}"; cd "$1"; writable_paths "$2" "$3"`,
      "_",
      repo,
      base,
      head,
    ],
    { encoding: "utf8" },
  );
  return out.split("\0").filter(Boolean).sort();
}

test("writable_paths is the head's diff minus protected, unwritable, lockfile, deleted and symlink paths", () => {
  const repo = mkdtempSync(join(tmpdir(), "writable-"));
  const git = (...args) =>
    execFileSync("git", ["-C", repo, ...args], { encoding: "utf8" });
  git("init", "-q", "-b", "main");
  git("config", "user.email", "t@t");
  git("config", "user.name", "t");
  const seed = {
    "a.txt": "base\n",
    "d.txt": "base\n",
    "src/b.py": "base\n",
    ".github/w.yaml": "base\n",
    ".claude/s.md": "base\n",
    "uv.lock": "base\n",
  };
  for (const [path, body] of Object.entries(seed)) {
    execFileSync("mkdir", ["-p", join(repo, dirname(path))]);
    writeFileSync(join(repo, path), body);
  }
  git("add", "-A");
  git("commit", "-q", "-m", "base");
  const base = git("rev-parse", "HEAD").trim();
  for (const path of Object.keys(seed))
    if (path !== "d.txt") writeFileSync(join(repo, path), "head\n");
  rmSync(join(repo, "d.txt"));
  execFileSync("ln", ["-s", "a.txt", join(repo, "ln.txt")]);
  git("add", "-A");
  git("commit", "-q", "-m", "head");
  assert.deepEqual(writablePaths(repo, base, "HEAD"), ["a.txt", "src/b.py"]);
});

test("the default protected set covers this template's Claude config and CI machinery, member by member", () => {
  const members = [
    ".claude/hooks/probe.txt",
    ".claude/skills/probe.txt",
    ".claude/settings.json",
    ".github/workflows/ci.yaml",
    ".github/scripts/probe.sh",
    ".github/actions/probe/action.yaml",
  ];
  for (const path of members) {
    assert.deepEqual(protectedMatches([path]), [path], `${path} is protected`);
  }
});

test("ordinary source and top-level files are NOT protected", () => {
  for (const path of ["setup.sh", "src/index.js", "infra/main.tf", "README.md"])
    assert.deepEqual(protectedMatches([path]), [], `${path} is not protected`);
});

test("protected_matches returns the protected SUBSET of a mixed list, in order", () => {
  assert.deepEqual(
    protectedMatches([
      "src/index.js",
      ".github/workflows/ci.yaml",
      "docs/a.md",
      ".claude/settings.json",
    ]),
    [".github/workflows/ci.yaml", ".claude/settings.json"],
  );
});

test("AUTO_RESOLVE_PROTECTED_RE widens the set for a repo with more sensitive trees", () => {
  const env = {
    AUTO_RESOLVE_PROTECTED_RE: "^(\\.claude/|\\.github/|infra/)",
  };
  assert.deepEqual(protectedMatches(["infra/main.tf"], env), ["infra/main.tf"]);
  assert.deepEqual(protectedMatches(["src/index.js"], env), []);
});

test("protected_matches on an empty list is empty, not an error", () => {
  assert.deepEqual(protectedMatches([]), []);
});

// The OAuth rung list is tested where it now lives, member by member, in
// tests/test_oauth_ladder.py — oauth-ladder.bash is the sole walk in the tree.

// The structural-skip set is a SILENT-DATA-LOSS floor: mergiraf reports a solve
// while dropping one side inside a YAML block scalar, or duplicating a TOML
// table. It is tested where it lives, because three callers read it — the
// prepare partition, structural_solve itself, and the info/attributes writer.
function structuralUnsafe(path, env = {}) {
  const rc = spawnSync(
    "bash",
    ["-c", `source "${LIB}"; structural_merge_unsafe "$1"`, "_", path],
    { encoding: "utf8", env: { ...process.env, ...env } },
  );
  return rc.status === 0;
}

test("the types mergiraf drops content on are refused, member by member", () => {
  for (const path of [
    "a.yaml",
    "a.yml",
    ".github/workflows/ci.yaml",
    "deep/nested/values.yml",
    "a.toml",
    "pyproject.toml",
  ])
    assert.equal(structuralUnsafe(path), true, `${path} must skip mergiraf`);
});

test("the types mergiraf merges safely still reach it", () => {
  for (const path of ["a.py", "a.json", "a.ts", "a.rs", "README.md", "a.sh"])
    assert.equal(
      structuralUnsafe(path),
      false,
      `${path} keeps the structural merge`,
    );
});

test("a name merely CONTAINING a skipped extension is not refused", () => {
  // The regex is anchored, so a real conflict in one of these still gets the
  // free structural pass instead of a paid model run.
  for (const path of ["a.yaml.py", "toml_parser.rs", "yaml/loader.go"])
    assert.equal(
      structuralUnsafe(path),
      false,
      `${path} keeps the structural merge`,
    );
});

test("an EMPTY override keeps the default, so no consumer can disable the floor by passing nothing", () => {
  assert.equal(
    structuralUnsafe("a.yaml", { AUTO_RESOLVE_STRUCTURAL_SKIP_RE: "" }),
    true,
  );
});

test("structural_solve REFUSES a skipped type even when mergiraf would report a solve", () => {
  // The belt for a future third caller: a fake mergiraf that always "solves"
  // must still not be able to rewrite a YAML file.
  const dir = mkdtempSync(join(tmpdir(), "structural-"));
  const fake = join(dir, "fake-mergiraf");
  writeFileSync(fake, '#!/usr/bin/env bash\necho "merged"\n', { mode: 0o755 });
  const conflicted = join(dir, "w.yaml");
  writeFileSync(conflicted, "a\n");
  const out = join(dir, "out");

  const rc = spawnSync(
    "bash",
    [
      "-c",
      `source "${LIB}"; structural_solve "$1" "$2" "$3"`,
      "_",
      fake,
      conflicted,
      out,
    ],
    { encoding: "utf8", env: process.env },
  );
  assert.notEqual(
    rc.status,
    0,
    "a YAML file must never report a structural solve",
  );

  // Non-vacuity: the same fake DOES solve a type that is not skipped.
  const safe = join(dir, "m.py");
  writeFileSync(safe, "a\n");
  const ok = spawnSync(
    "bash",
    [
      "-c",
      `source "${LIB}"; structural_solve "$1" "$2" "$3"`,
      "_",
      fake,
      safe,
      out,
    ],
    { encoding: "utf8", env: process.env },
  );
  assert.equal(ok.status, 0, "a .py file still reaches the structural merge");
  rmSync(dir, { recursive: true, force: true });
});

test("every skip GLOB names an extension the skip REGEX also matches", () => {
  // The ERE is DERIVED from these globs, so this pins the derivation's one
  // assumption: every member is a bare `*.<ext>`, which is all `${glob#*.}`
  // can encode. A `Makefile`-shaped entry would silently build a broken regex.
  const globs = execFileSync(
    "bash",
    ["-c", `source "${LIB}"; printf '%s\\n' "\${STRUCTURAL_SKIP_GLOBS[@]}"`],
    { encoding: "utf8", env: process.env },
  )
    .split("\n")
    .filter(Boolean);
  assert.ok(globs.length > 0, "the glob list is not empty");
  for (const glob of globs) {
    assert.ok(glob.startsWith("*."), `${glob} is an extension glob`);
    assert.equal(
      structuralUnsafe(glob.replaceAll("*", "probe")),
      true,
      `${glob} is covered by the skip regex`,
    );
  }
});

test("override_unsafe_merge_attributes leaves a consumer's `-merge` LOCKFILE refusing to merge", () => {
  // info/attributes outranks the WHOLE stack, so a blanket `*.yaml merge=text`
  // there would beat `pnpm-lock.yaml -merge` and re-enable the line merge that
  // rule exists to refuse — turning is_unmergeable from true to false and
  // landing bytes neither manifest produces. Narrowing to paths already bound
  // to mergiraf is what prevents it.
  const dir = mkdtempSync(join(tmpdir(), "lockattrs-"));
  const git = (...args) =>
    execFileSync("git", args, { cwd: dir, encoding: "utf8", env: process.env });
  git("init", "-q");
  git("config", "user.email", "t@t");
  git("config", "user.name", "t");
  writeFileSync(
    join(dir, ".gitattributes"),
    "*.yaml merge=mergiraf\n*.yml merge=mergiraf\n*.toml merge=mergiraf\n" +
      "pnpm-lock.yaml -merge\nuv.lock -merge\n",
  );
  for (const f of ["pnpm-lock.yaml", "uv.lock", "w.yaml", "p.toml"])
    writeFileSync(join(dir, f), "x\n");
  git("add", "-A");
  git("commit", "-qm", "base");

  execFileSync(
    "bash",
    [
      "-c",
      `source "${LIB}"; cd "$1"; override_unsafe_merge_attributes`,
      "_",
      dir,
    ],
    { encoding: "utf8", env: process.env },
  );

  const attr = (f) =>
    git("check-attr", "merge", "--", f)
      .trim()
      .replace(/^.*: merge: /, "");
  // The whole point: these must NOT become line-mergeable.
  assert.equal(
    attr("pnpm-lock.yaml"),
    "unset",
    "a -merge lockfile stays unset",
  );
  assert.equal(attr("uv.lock"), "unset");
  // And the types that WERE going to mergiraf are redirected.
  assert.equal(attr("w.yaml"), "text");
  assert.equal(attr("p.toml"), "text");
  rmSync(dir, { recursive: true, force: true });
});

test("override_unsafe_merge_attributes covers a NON-ASCII path git would C-quote", () => {
  // Under the default core.quotepath, `git ls-files` prints such a name
  // wrapped in double quotes with its bytes octal-escaped. Feeding that
  // literal to check-attr matches nothing, so the file silently kept its
  // mergiraf binding for the whole merge. -z gives the raw bytes.
  const dir = mkdtempSync(join(tmpdir(), "utf8attrs-"));
  const git = (...args) =>
    execFileSync("git", args, { cwd: dir, encoding: "utf8", env: process.env });
  git("init", "-q");
  git("config", "user.email", "t@t");
  git("config", "user.name", "t");
  writeFileSync(join(dir, ".gitattributes"), "*.yaml merge=mergiraf\n");
  writeFileSync(join(dir, "caf\u00e9.yaml"), "x\n");
  git("add", "-A");
  git("commit", "-qm", "base");
  execFileSync(
    "bash",
    [
      "-c",
      `source "${LIB}"; cd "$1"; override_unsafe_merge_attributes`,
      "_",
      dir,
    ],
    { encoding: "utf8", env: process.env },
  );
  assert.match(
    git("check-attr", "merge", "--", "caf\u00e9.yaml").trim(),
    /merge: text$/,
    "a non-ASCII path must not keep the structural driver",
  );
  rmSync(dir, { recursive: true, force: true });
});

test("override_unsafe_merge_attributes covers a consumer on merge.default=mergiraf", () => {
  // An unspecified `merge` takes merge.default — the same gitattributes(5) rule
  // the .gitattributes block relies on. Such a path reports `unspecified`, so a
  // filter keyed only on `mergiraf` skipped it while git still ran mergiraf.
  const dir = mkdtempSync(join(tmpdir(), "defaultattrs-"));
  const git = (...args) =>
    execFileSync("git", args, { cwd: dir, encoding: "utf8", env: process.env });
  git("init", "-q");
  git("config", "user.email", "t@t");
  git("config", "user.name", "t");
  git("config", "merge.default", "mergiraf");
  // No `*.yaml` line at all — the binding comes from merge.default alone.
  writeFileSync(join(dir, ".gitattributes"), "pnpm-lock.yaml -merge\n");
  for (const f of ["w.yaml", "Config.YAML", "pnpm-lock.yaml", "keep.py"])
    writeFileSync(join(dir, f), "x\n");
  git("add", "-A");
  git("commit", "-qm", "base");
  execFileSync(
    "bash",
    [
      "-c",
      `source "${LIB}"; cd "$1"; override_unsafe_merge_attributes`,
      "_",
      dir,
    ],
    { encoding: "utf8", env: process.env },
  );
  const attr = (f) =>
    git("check-attr", "merge", "--", f)
      .trim()
      .replace(/^.*: merge: /, "");
  assert.equal(attr("w.yaml"), "text");
  // Case-insensitively too: a `*.yaml` PATHSPEC would never have listed this.
  assert.equal(attr("Config.YAML"), "text");
  // The lockfile narrowing survives: `-merge` is `unset`, never `unspecified`.
  assert.equal(attr("pnpm-lock.yaml"), "unset");
  // And a type the structural merge handles safely is left alone entirely.
  assert.equal(attr("keep.py"), "unspecified");
  rmSync(dir, { recursive: true, force: true });
});

test("override_unsafe_merge_attributes handles a path containing a space", () => {
  const dir = mkdtempSync(join(tmpdir(), "spaceattrs-"));
  const git = (...args) =>
    execFileSync("git", args, { cwd: dir, encoding: "utf8", env: process.env });
  git("init", "-q");
  git("config", "user.email", "t@t");
  git("config", "user.name", "t");
  writeFileSync(join(dir, ".gitattributes"), "*.yaml merge=mergiraf\n");
  writeFileSync(join(dir, "sp ace.yaml"), "x\n");
  git("add", "-A");
  git("commit", "-qm", "base");
  execFileSync(
    "bash",
    [
      "-c",
      `source "${LIB}"; cd "$1"; override_unsafe_merge_attributes`,
      "_",
      dir,
    ],
    { encoding: "utf8", env: process.env },
  );
  assert.match(
    git("check-attr", "merge", "--", "sp ace.yaml").trim(),
    /merge: text$/,
  );
  rmSync(dir, { recursive: true, force: true });
});

test("the skip set matches regardless of extension CASE", () => {
  // `mergiraf solve` keys on the filename and reads no attribute, so an
  // uppercase extension reaches the drop where git's own globs would miss it.
  for (const path of ["Config.YAML", "a.Yml", "P.TOML"])
    assert.equal(structuralUnsafe(path), true, `${path} must skip mergiraf`);
});

test("override_unsafe_merge_attributes outranks a consumer's own merge=mergiraf binding", () => {
  const dir = mkdtempSync(join(tmpdir(), "attrs-"));
  const git = (...args) =>
    execFileSync("git", args, { cwd: dir, encoding: "utf8", env: process.env });
  git("init", "-q");
  git("config", "user.email", "t@t");
  git("config", "user.name", "t");
  // The consumer tree this action cannot edit.
  writeFileSync(join(dir, ".gitattributes"), "*.yaml merge=mergiraf\n");
  writeFileSync(join(dir, "w.yaml"), "steps:\n  - run: |\n      echo base\n");
  git("add", "-A");
  git("commit", "-qm", "base");

  const before = git("check-attr", "merge", "--", "w.yaml").trim();
  assert.match(
    before,
    /merge: mergiraf$/,
    "the consumer binding is live first",
  );

  execFileSync(
    "bash",
    [
      "-c",
      `source "${LIB}"; cd "$1"; override_unsafe_merge_attributes`,
      "_",
      dir,
    ],
    { encoding: "utf8", env: process.env },
  );

  const after = git("check-attr", "merge", "--", "w.yaml").trim();
  assert.match(after, /merge: text$/, "info/attributes wins");
  rmSync(dir, { recursive: true, force: true });
});

function committedMarkerPaths(dir, baseSha) {
  const out = execFileSync(
    "bash",
    [
      "-c",
      `source "${LIB}"; cd "$1"; committed_marker_paths "$2"`,
      "_",
      dir,
      baseSha,
    ],
    { encoding: "utf8", env: process.env },
  );
  return out.split("\n").filter(Boolean);
}

test("committed_marker_paths exempts a fixture file whose marker block is unchanged from base", () => {
  const dir = mkdtempSync(join(tmpdir(), "markers-fixture-"));
  const git = (...args) =>
    execFileSync("git", args, { cwd: dir, encoding: "utf8", env: process.env });
  git("init", "-q");
  git("config", "user.email", "t@t");
  git("config", "user.name", "t");
  const fixture =
    "test data:\n<<<<<<< local\na\n=======\nb\n>>>>>>> template\n";
  writeFileSync(join(dir, "fixture.txt"), fixture);
  git("add", "-A");
  git("commit", "-qm", "base");
  const baseSha = git("rev-parse", "HEAD").trim();

  // Unrelated edit elsewhere in the file; the marker block itself is untouched.
  writeFileSync(join(dir, "fixture.txt"), `${fixture}trailer\n`);
  git("add", "-A");
  git("commit", "-qm", "unrelated edit");

  assert.deepEqual(committedMarkerPaths(dir, baseSha), []);
  rmSync(dir, { recursive: true, force: true });
});

test("committed_marker_paths flags a NEW marker block in a file whose base copy already had a fixture block", () => {
  // Regression: any base-side marker triple used to exempt the WHOLE file,
  // hiding a genuinely new, unrelated conflict added to it later.
  const dir = mkdtempSync(join(tmpdir(), "markers-newblock-"));
  const git = (...args) =>
    execFileSync("git", args, { cwd: dir, encoding: "utf8", env: process.env });
  git("init", "-q");
  git("config", "user.email", "t@t");
  git("config", "user.name", "t");
  const fixtureBlock = "<<<<<<< local\na\n=======\nb\n>>>>>>> template\n";
  writeFileSync(join(dir, "fixture.txt"), `doc:\n${fixtureBlock}`);
  git("add", "-A");
  git("commit", "-qm", "base");
  const baseSha = git("rev-parse", "HEAD").trim();

  const newBlock = "<<<<<<< local\nx=5\n=======\nx=3\n>>>>>>> template\n";
  writeFileSync(
    join(dir, "fixture.txt"),
    `doc:\n${fixtureBlock}\nreal conflict:\n${newBlock}`,
  );
  git("add", "-A");
  git("commit", "-qm", "sync adds a real conflict");

  assert.deepEqual(committedMarkerPaths(dir, baseSha), ["fixture.txt"]);
  rmSync(dir, { recursive: true, force: true });
});

// split_fragment_collisions reads `:2:` and `:3:` of every fragment it calls an
// add/add, so a fragment shape that only has ONE of those stages aborts the whole
// prepare step under `set -e`. A rename each side made to a different name is the
// state that produces one: git writes `DD` for the original and `AU`/`UA` for the
// two new names, all three under changelog.d/.
test("split_fragment_collisions leaves a ONE-SIDED changelog fragment alone", () => {
  const repo = mkdtempSync(join(tmpdir(), "fragment-split-"));
  const git = (...args) =>
    execFileSync("git", ["-C", repo, ...args], { encoding: "utf8" });
  git("init", "-q", "-b", "main");
  git("config", "user.email", "t@t");
  git("config", "user.name", "t");
  execFileSync("mkdir", ["-p", join(repo, "changelog.d")]);
  // Long enough for rename detection to fire, which is what makes git call this
  // a conflict rather than an unrelated add and delete.
  const body = Array.from({ length: 40 }, (_, n) => `line ${n}\n`).join("");
  writeFileSync(join(repo, "changelog.d/aaaa.fix.md"), body);
  git("add", "-A");
  git("commit", "-qm", "the shared fragment");
  git("checkout", "-q", "-b", "other");
  git("mv", "changelog.d/aaaa.fix.md", "changelog.d/bbbb.fix.md");
  git("commit", "-qam", "their rename");
  git("checkout", "-q", "main");
  git("mv", "changelog.d/aaaa.fix.md", "changelog.d/cccc.fix.md");
  git("commit", "-qam", "our rename");
  spawnSync("git", ["-C", repo, "merge", "--no-commit", "other"], {
    encoding: "utf8",
  });

  const before = git("status", "--porcelain");
  assert.match(before, /^DD changelog\.d\/aaaa\.fix\.md$/m);
  assert.match(before, /^AU changelog\.d\/cccc\.fix\.md$/m);
  assert.match(before, /^UA changelog\.d\/bbbb\.fix\.md$/m);

  // `set -e` is prepare.sh's own mode: a failed `git cat-file` here does not
  // report, it ends the step after the merge and before any resolution.
  const split = (withFacts) =>
    spawnSync(
      "bash",
      [
        "-c",
        `set -e
source "${LIB}"
cd "$1"
mapfile -d "" -t unmerged < <(git diff -z --name-only --diff-filter=U)
${withFacts ? `load_path_facts . HEAD "" "\${unmerged[@]}"` : ""}
split_fragment_collisions`,
        "_",
        repo,
      ],
      { encoding: "utf8" },
    );

  const done = split(true);
  assert.equal(done.status, 0, done.stderr);
  // Untouched: no side was split, and no third fragment was invented for one.
  assert.equal(git("status", "--porcelain"), before);

  // An unclassified path reads through `has_fact` exactly as one that is not an
  // add/add, so the refusal is what keeps the two-stage read off a fragment
  // nobody asked about.
  const unasked = split(false);
  assert.notEqual(unasked.status, 0, "an unclassified fragment must refuse");
  assert.match(unasked.stderr, /no classification for the conflicted fragment/);
  rmSync(repo, { recursive: true, force: true });
});
