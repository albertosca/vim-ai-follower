#!/bin/zsh
# QA Check 9 — nvim standalone (no tmux). Must be run from a plain terminal
# OUTSIDE any tmux session. Sets config to the nvim backend, starts a
# follower (which opens a real standalone window, not a tmux pane), and
# fires the standalone-demo.py fixture over a hook pre/post pair. Backs up
# any real config.json first via qa_protect_config (auto-restores on any
# crash; the driver's printed cleanup command restores it once Alberto is
# done watching) — this is the only check that touches
# ~/.config/claude-vim-follower/config.json.
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

qa_protect_config
trap '_qa_restore_config_on_exit' EXIT
qa_write_test_config <<'EOF'
{"backend": "nvim", "nvim_window": "auto"}
EOF
echo ">>> Wrote nvim-backend config to $QA_CONFIG_PATH"

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
  echo "Your real config is being restored automatically now."
  exit "$CODE"
fi

F=/tmp/vaf-qa-standalone.py
cp "$FIX/standalone-demo.py" "$F"
P="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$F\"},\"session_id\":\"me\"}"
echo "$P" | "$CF" hook pre
echo ">>> Firing hook post — should animate into a separate, visible window."
echo "$P" | "$CF" hook post &

# From here on, the follower is live and Alberto needs the test config to
# stay in place while he watches — disarm the exit-trap's auto-restore so it
# doesn't fire the moment this script's own shell falls off the end.
qa_config_handoff

echo
echo "LOOK AT: whether a new nvim surface opens beside your current work — a"
echo "GUI window (nvim-qt/VimR if installed), a split pane inside your"
echo "current iTerm tab (iTerm2 default fallback), or a fresh Terminal.app"
echo "window (last-resort fallback) — whether the origin terminal/pane keeps"
echo "working while it animates, and whether it's still there once the edit"
echo "finishes (it must NOT auto-close)."
echo
echo "Then run 'claude-follow stop' from this terminal — it should quit the"
echo "standalone window cleanly."
echo
echo "Cleanup when done:"
echo "  rm -f $F"
if [[ "$QA_CONFIG_HAD_REAL" -eq 1 ]]; then
  echo "  cp $QA_CONFIG_BACKUP $QA_CONFIG_PATH   # restore your real config"
else
  echo "  rm -f $QA_CONFIG_PATH   # no real config existed before this check"
fi
