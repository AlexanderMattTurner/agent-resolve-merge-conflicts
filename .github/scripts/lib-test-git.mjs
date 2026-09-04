// The one `git` the auto-resolve node suites use to drive a fixture repo.
// It throws on a nonzero exit, so a failed setup step surfaces where it
// happened instead of later, as a mismatched assertion.

import { execFileSync } from "node:child_process";

/**
 * Run a git command against a repository and return its stdout.
 * @param {string} cwd the repository to run the command in.
 * @param {...string} args the git subcommand and its arguments.
 * @returns {string} the command's stdout.
 */
export function git(cwd, ...args) {
  return execFileSync("git", ["-C", cwd, ...args], { encoding: "utf8" });
}
