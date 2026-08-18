#!/bin/bash
# One-command setup for the Claude automation template

set -euo pipefail

echo "Setting up Claude automation template..."

# Configure git hooks
git config core.hooksPath .hooks

if [[ -f package.json ]]; then
  # Route through corepack so the pnpm version actually used matches the
  # "packageManager" pin in package.json — a bare `pnpm` on PATH (e.g. from
  # `npm install -g pnpm`) bypasses that pin and can rewrite the lockfile
  # into an off-version format.
  if command -v corepack &>/dev/null; then
    corepack enable
  else
    # Pin the fallback install to the "packageManager" version so a bare
    # `npm install -g pnpm` can't pull a newer/older pnpm that rewrites the
    # lockfile into an off-version format — the exact hazard corepack avoids.
    pnpm_spec=$(node -e 'process.stdout.write(require("./package.json").packageManager || "pnpm")')
    echo "Installing ${pnpm_spec}..."
    npm install -g "$pnpm_spec"
  fi

  # Install dependencies (postinstall also sets core.hooksPath, redundantly)
  pnpm install
fi

# Install Python dependencies if applicable
if [[ -f uv.lock ]] && command -v uv &>/dev/null; then
  uv sync
fi

# Register the syntax-aware merge driver .gitattributes names. Those attributes
# are inert until the checkout doing the merge has mergiraf on PATH and
# merge.mergiraf.driver in its git config: git reports nothing and line-merges
# instead. install-mergiraf.sh registers the driver only after verifying the
# download's digest and proving its `solve -p` contract.
if [[ -x .github/scripts/install-mergiraf.sh && "$(uname -s) $(uname -m)" = "Linux x86_64" ]]; then
  # The pinned asset is linux_amd64 and is read with sha256sum, so no other host
  # gets an install — it keeps git's line merge, exactly as before.
  mergiraf_dest="/usr/local/bin"
  case ":${PATH}:" in
  *":${HOME}/.local/bin:"*) mergiraf_dest="${HOME}/.local/bin" ;;
  esac
  echo "Installing mergiraf (pinned, digest-verified) into ${mergiraf_dest}..."
  bash .github/scripts/install-mergiraf.sh "$mergiraf_dest" ||
    echo "⚠ mergiraf install failed — this checkout keeps git's line merge" >&2
fi

# Verify setup
if [[ "$(git config core.hooksPath)" = ".hooks" ]]; then
  echo ""
  echo "✓ Setup complete!"
  echo ""
  echo "Next steps:"
  echo "  1. Edit CLAUDE.md with your project details"
  if [[ -f package.json ]]; then
    echo "  2. Configure scripts in package.json"
  fi
  echo "  Start coding!"
else
  echo ""
  echo "⚠ Error: Git hooks are not configured correctly (core.hooksPath != .hooks)." >&2
  echo "  Run: git config core.hooksPath .hooks" >&2
  exit 1
fi
