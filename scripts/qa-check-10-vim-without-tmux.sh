#!/bin/zsh
# QA Check 10 — vim-without-tmux error. Must be run from a plain terminal
# OUTSIDE any tmux session. Sets config to the tmux backend and starts the
# follower, which must fail loudly and actionably instead of no-opping or
# crashing. Trivial by design (a pure inline command sequence per the
# runbook) — this thin script exists only for consistency with the other
# checks (run-id bookkeeping, config backup/restore, LOOK AT summary).
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

CONFIG_DIR=~/.config/claude-vim-follower
CONFIG="$CONFIG_DIR/config.json"
BACKUP="/tmp/vaf-qa-${RUN_ID}-config-backup.json"
mkdir -p "$CONFIG_DIR"

if [[ -f "$CONFIG" ]]; then
  cp "$CONFIG" "$BACKUP"
  echo ">>> Backed up your real config to $BACKUP"
  HAD_CONFIG=1
else
  HAD_CONFIG=0
fi

cat > "$CONFIG" <<'EOF'
{"backend": "tmux"}
EOF
echo ">>> Wrote tmux-backend config to $CONFIG"

CF="$REPO/bin/claude-follow"

echo ">>> Check 10 — vim-without-tmux error"
echo ">>> Running: claude-follow start"
set +e
"$CF" start
CODE=$?
set -e
echo "exit code: $CODE"

echo
echo "LOOK AT: the terminal output and the exit code above; whether any"
echo "pane/window opened anywhere. Expect the exact message"
echo "  claude-follow: the vim backend requires tmux — run inside a tmux session, or set backend to nvim"
echo "printed to stdout, exit code non-zero (observed: 1), and no follower"
echo "pane/window opening anywhere."
echo
echo "Cleanup when done:"
if [[ "$HAD_CONFIG" -eq 1 ]]; then
  echo "  cp $BACKUP $CONFIG   # restore your real config"
else
  echo "  rm -f $CONFIG   # no real config existed before this check"
fi
