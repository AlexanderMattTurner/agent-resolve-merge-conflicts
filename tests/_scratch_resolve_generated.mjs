// A stand-in for the CALLING repository's derived-file resolver.
//
// This resolver ships none: `prepare.sh` takes the caller's module through
// `AUTO_RESOLVE_RESOLVER_MJS` and the command that runs it through
// `AUTO_RESOLVE_PRE_PASS`. So the end-to-end tests need a module on the other
// side of that contract, and this is it — the smallest one that answers every
// question `prepare.sh` and `bundle.py` ask of a caller's resolver.
//
// Rule shape, as those scripts assume it:
//   { owns: [path…], sources: [path…], command: [argv…] | generator: <path> }
// A rule is selected when a path it owns is conflicted. Its outputs are DEFERRED
// when a source is conflicted too, RESOLVED when the rerun leaves clean bytes,
// and FAILED when the rerun crashes, deletes the output, or leaves markers.

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

export function readFlag(argv, name) {
  const prefix = `--${name}=`;
  const match = argv.find((arg) => arg.startsWith(prefix));
  return match === undefined ? undefined : match.slice(prefix.length);
}

// The install a `generator` rule needs before it runs: the generator resolves
// imports out of node_modules, and the merged manifest may ask for a dependency
// the pre-merge tree never installed.
const INSTALL_MERGED_DEPS = [
  "pnpm",
  "install",
  "--frozen-lockfile",
  "--ignore-scripts",
];

function run(root, command) {
  execFileSync(command[0], command.slice(1), { cwd: root, stdio: "pipe" });
}

function conflictedPaths(root) {
  const out = execFileSync("git", ["diff", "--name-only", "--diff-filter=U"], {
    cwd: root,
    encoding: "utf8",
  });
  return out.split("\n").filter(Boolean);
}

function ruleCommand(rule, root) {
  if (rule.command) return rule.command;
  if (rule.generator) return [process.execPath, join(root, rule.generator)];
  throw new Error(`rule owning ${rule.owns.join(", ")} has no command`);
}

// A failed install re-throws on EVERY later call: letting a later generator run
// against the stale pre-merge node_modules would stage its output as resolved
// with the wrong dependency set.
function mergedDepsInstaller(root) {
  let outcome = "pending";
  return () => {
    if (outcome === "done") return;
    if (outcome !== "pending") throw outcome;
    if (!existsSync(join(root, "package.json"))) {
      outcome = "done";
      return;
    }
    try {
      run(root, INSTALL_MERGED_DEPS);
    } catch (error) {
      outcome = new Error(`merged-deps install failed: ${error.message}`);
      throw outcome;
    }
    outcome = "done";
  };
}

// Every `command` rule (the lockfile re-derivations) runs before any `generator`
// rule, because the install between the two phases reads the lockfile a command
// rule has just re-derived from the merged manifests.
function commandRulesFirst(rules) {
  return [
    ...rules.filter((r) => r.command),
    ...rules.filter((r) => !r.command),
  ];
}

export function resolveGenerated({ root = process.cwd(), rules = [] } = {}) {
  const conflicted = new Set(conflictedPaths(root));
  const resolved = [];
  const deferred = [];
  const failed = [];
  const installMergedDeps = mergedDepsInstaller(root);

  for (const rule of commandRulesFirst(rules)) {
    const owned = rule.owns.filter((path) => conflicted.has(path));
    if (owned.length === 0) continue;
    // A conflicted source means the merged source is not trustworthy yet, so the
    // derived files wait until the LLM has resolved it.
    if ((rule.sources ?? []).some((source) => conflicted.has(source))) {
      deferred.push(...owned.map((path) => ({ path })));
      continue;
    }
    try {
      if (rule.generator !== undefined) installMergedDeps();
      run(root, ruleCommand(rule, root));
    } catch (error) {
      failed.push(...owned.map((path) => ({ path, reason: error.message })));
      continue;
    }
    const clean = owned.filter((path) => {
      if (!existsSync(join(root, path))) {
        failed.push({ path, reason: "re-derived output is missing" });
        return false;
      }
      if (!/^<{7}(?: |$)/m.test(readFileSync(join(root, path), "utf8")))
        return true;
      failed.push({ path, reason: "re-derived output still contains markers" });
      return false;
    });
    if (clean.length)
      execFileSync("git", ["add", "--", ...clean], {
        cwd: root,
        stdio: "pipe",
      });
    resolved.push(...clean);
  }
  return { resolved, deferred, failed };
}
