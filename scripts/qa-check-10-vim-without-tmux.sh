#!/bin/zsh
# QA Check 10 — vim-without-tmux error. Must be run from a plain terminal
# OUTSIDE any tmux session. Sets config to the tmux backend and starts the
# follower, which must fail loudly and actionably instead of no-opping or
# crashing. Trivial by design (a pure inline command sequence per the
# runbook) — this thin script exists only for consistency with the other
# checks (run-id bookkeeping, config backup/restore via qa_protect_config,
# LOOK AT summary).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
source "$HERE/qa-lib.sh"

if [[ -n "$TMUX" ]]; then
  echo "ERROR: run this from a plain terminal OUTSIDE any tmux session (Terminal.app/iTerm)."
  exit 1
fi

RUN_ID=$(qa_run_id)
qa_snapshot_cache
echo "QA run id: $RUN_ID"

qa_protect_config
trap '_qa_restore_config_on_exit' EXIT
qa_write_test_config <<'EOF'
{"backend": "tmux"}
EOF
echo ">>> Wrote tmux-backend config to $QA_CONFIG_PATH"

CF="$REPO/bin/claude-follow"

echo ">>> Check 10 — vim-without-tmux error"
echo ">>> Running: claude-follow start"
set +e
"$CF" start
CODE=$?
set -e
echo "exit code: $CODE"

# The check is fully done at this point (no live follower to keep watching),
# but hand off restore-timing to the driver anyway for consistency with
# Check 9's config-touching checks — same printed cleanup step either way.
qa_config_handoff

echo
echo "LOOK AT: the terminal output and the exit code above; whether any"
echo "pane/window opened anywhere. Expect the exact message"
echo "  claude-follow: the vim backend requires tmux — run inside a tmux session, or set backend to nvim"
echo "printed to stdout, exit code non-zero (observed: 1), and no follower"
echo "pane/window opening anywhere."
echo
echo "Cleanup when done:"
if [[ "$QA_CONFIG_HAD_REAL" -eq 1 ]]; then
  echo "  cp $QA_CONFIG_BACKUP $QA_CONFIG_PATH   # restore your real config"
else
  echo "  rm -f $QA_CONFIG_PATH   # no real config existed before this check"
fi
