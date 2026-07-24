#!/bin/zsh
# QA Check 9 — nvim standalone (no tmux). Must be run from a plain terminal
# OUTSIDE any tmux session. Sets config to the nvim backend, starts a
# follower (which opens a real standalone window, not a tmux pane), and
# fires the standalone-demo.py fixture over a hook pre/post pair. Backs up
# any real config.json first and prints how to restore it — this is the only
# check that touches ~/.config/claude-vim-follower/config.json.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
FIX="$REPO/qa/fixtures"
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
{"backend": "nvim", "nvim_window": "auto"}
EOF
echo ">>> Wrote nvim-backend config to $CONFIG"

CF="$REPO/bin/claude-follow"

echo ">>> Check 9 — nvim standalone (no tmux)"
echo ">>> Starting (claude-follow start)..."
set +e
"$CF" start
CODE=$?
set -e
if [[ "$CODE" -ne 0 ]]; then
  echo
  echo "claude-follow start failed (exit $CODE) — see its error above. Not a QA"
  echo "PASS/FAIL by itself (Check 9's FAIL case is 'no window opens' with a"
  echo "traceback, which this could be) but nothing was set up to watch."
  echo "Cleanup: restoring your config now."
  if [[ "$HAD_CONFIG" -eq 1 ]]; then
    cp "$BACKUP" "$CONFIG"
  else
    rm -f "$CONFIG"
  fi
  exit "$CODE"
fi

F=/tmp/vaf-qa-standalone.py
cp "$FIX/standalone-demo.py" "$F"
P="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$F\"},\"session_id\":\"me\"}"
echo "$P" | "$CF" hook pre
echo ">>> Firing hook post — should animate into a separate, visible window."
echo "$P" | "$CF" hook post &

echo
echo "LOOK AT: whether a separate, visible window opens (GUI nvim-qt/VimR if"
echo "installed, else a fresh Terminal.app window) beside — not inside — the"
echo "origin terminal; whether the origin terminal keeps working while it"
echo "animates; whether the window is still open once the edit finishes"
echo "(it must NOT auto-close)."
echo
echo "Then run 'claude-follow stop' from this terminal — it should quit the"
echo "standalone window cleanly."
echo
echo "Cleanup when done:"
echo "  rm -f $F"
if [[ "$HAD_CONFIG" -eq 1 ]]; then
  echo "  cp $BACKUP $CONFIG   # restore your real config"
else
  echo "  rm -f $CONFIG   # no real config existed before this check"
fi
