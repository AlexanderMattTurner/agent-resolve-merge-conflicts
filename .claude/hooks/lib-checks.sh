#!/bin/bash
# Shared helpers for Claude Code hook scripts

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$PROJECT_DIR" || exit 1

exists() { command -v "$1" &>/dev/null; }

# The predicate behind has_script, resolved beside this library rather than
# under the project root: template-sync delivers .claude/ and .github/scripts/
# together, and a hook must not depend on the project it inspects carrying CI
# plumbing of its own.
_SCRIPT_CONFIGURED="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.github/scripts" && pwd)/script-configured.sh"

# has_script NAME — is the package.json script NAME configured? One
# implementation, in script-configured.sh; a second copy here drifted from it on
# what a malformed manifest means.
#
# Exit 2 rather than return it: "could not classify" must stop the run, not read
# as "not configured" and silently skip real pre-push checks. Every such status
# collapses to 2, because the caller's contract distinguishes 1 from >=2 and
# nothing downstream reads the individual codes.
has_script() {
  local rc=0
  bash "$_SCRIPT_CONFIGURED" "$1" || rc=$?
  ((rc >= 2)) && exit 2
  return "$rc"
}
