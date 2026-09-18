#!/bin/bash
# Shared fail-closed helper for the git hooks in .hooks/.
#
# A gate that cannot run its tool must FAIL the git operation, not silently
# exit 0 — a silent skip lets unchecked work reach the branch with no signal
# that anything was bypassed. Sourced (not executed), so gate_die_missing_tool
# exits the calling hook.

# gate_die_missing_tool <hook-name> <tool> <install-hint>: loud stderr + exit 1.
gate_die_missing_tool() {
  local hook=$1 tool=$2 hint=$3
  echo "$hook: required tool '$tool' not found — REFUSING to continue rather than skip its checks." >&2
  echo "$hook: $hint" >&2
  exit 1
}

# gate_tool_root: absolute path of the MAIN checkout, where provisioned,
# gitignored tooling (node_modules/, .venv/) actually lives. In a linked git
# worktree, --show-toplevel is the worktree's own root, which has neither —
# they're installed once, only in the main checkout. --git-common-dir is
# shared by every worktree and always ends in /.git, so its parent is that
# main checkout (and is --show-toplevel itself outside a worktree).
gate_tool_root() {
  local common_dir
  common_dir=$(git rev-parse --path-format=absolute --git-common-dir)
  echo "${common_dir%/*}"
}
