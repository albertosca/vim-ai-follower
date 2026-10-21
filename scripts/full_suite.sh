#!/usr/bin/env bash
# The ONE full-suite command (real tmux + Vim + nvim, ~7 min). Its coverage gate is the
# source scripts/check_readme_facts.py reads for the "100% branch coverage" claim.
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/validate_plugin.sh
# -W error: a connection, socket or child left to the garbage collector fails
# the run instead of warning in whichever test the collector happens to hit.
exec uv run pytest -q -W error --cov=vim_ai_follower --cov-branch --cov-fail-under=100 "$@"
