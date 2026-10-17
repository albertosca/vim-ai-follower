#!/usr/bin/env bash
# `claude plugin validate --strict .` as a local gate (not CI: it needs the
# Claude Code CLI). Tolerates exactly one warning: CLAUDE.md at the plugin
# root, which is this repo's notes for Claude Code, not plugin context.
# Any error, or any other warning, fails. Without `claude` on PATH it skips.
set -uo pipefail
cd "$(dirname "$0")/.."

tolerated="root: CLAUDE.md at the plugin root is not loaded as project context."

if ! command -v claude >/dev/null 2>&1; then
  echo "validate_plugin: claude not on PATH, skipping the plugin validation" >&2
  exit 0
fi

if ! claude plugin validate . >/dev/null 2>&1; then
  claude plugin validate .
  echo "validate_plugin: the plugin has validation errors" >&2
  exit 1
fi

strict_output="$(claude plugin validate --strict . 2>&1)"
strict_status=$?
if [ "$strict_status" -eq 0 ]; then
  exit 0
fi

if printf '%s\n' "$strict_output" | grep -q '⚠ Found 1 warning:' \
  && printf '%s\n' "$strict_output" | grep -qF "$tolerated"; then
  echo "validate_plugin: passed; the root CLAUDE.md warning is tolerated" >&2
  exit 0
fi

printf '%s\n' "$strict_output"
echo "validate_plugin: a warning other than the tolerated root CLAUDE.md one" >&2
exit 1
