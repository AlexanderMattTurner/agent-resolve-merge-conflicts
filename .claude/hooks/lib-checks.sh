#!/bin/bash
# Shared helpers for Claude Code hook scripts

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$PROJECT_DIR" || exit 1

exists() { command -v "$1" &>/dev/null; }

# The predicate behind has_script, resolved beside this library rather than
# under the project root: template-sync delivers .claude/ and .github/scripts/
# together, and a hook must not depend on the project it inspects carrying CI
# plumbing of its own. Parameter expansion, not `dirname`: a pre-push hook runs
# under whatever PATH the caller has, and nothing here may need one.
_SCRIPT_CONFIGURED="${BASH_SOURCE[0]%/*}/../../.github/scripts/script-configured.sh"

# has_script NAME — is the package.json script NAME configured? Delegates to
# script-configured.sh, the one implementation of that predicate.
#
# Exit 2 rather than return it: "could not classify" must stop the run, not read
# as "not configured" and silently skip real pre-push checks. Every such status
# collapses to 2, because the caller's contract distinguishes 1 from >=2 and
# nothing downstream reads the individual codes.
has_script() {
  if [[ ! -f "$_SCRIPT_CONFIGURED" ]]; then
    echo "ERROR: lib-checks.sh: has_script needs $_SCRIPT_CONFIGURED, which is missing" >&2
    exit 2
  fi
  local rc=0
  # $BASH, not `bash`: the interpreter already running, so no PATH lookup.
  "$BASH" "$_SCRIPT_CONFIGURED" "$1" || rc=$?
  ((rc >= 2)) && exit 2
  return "$rc"
}
