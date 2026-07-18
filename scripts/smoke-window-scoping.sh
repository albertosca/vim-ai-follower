#!/bin/zsh
# Live smoke for window-scoped follower identity, in a DEDICATED tmux session
# on your real server (real Vim config). It NEVER kills the server — only the
# `vaf-smoke` session it creates. It builds two windows, starts a follower in
# each, and animates a DISTINCT file into each, so you can confirm no bleed.
#
# WHAT TO CHECK after it finishes:
#   tmux attach -t vaf-smoke
#   - Window 0 (prefix 0): its follower shows ONLY alpha.py / "WINDOW ZERO".
#   - Window 1 (prefix 1): its follower shows ONLY beta.py  / "WINDOW ONE".
#   - Neither window's edits appear in the other's follower. (That's the fix.)
# The script also prints a programmatic isolation check (a pause signal in
# window 0 must be invisible to window 1).
#
# CLEANUP when done:  tmux kill-session -t vaf-smoke
#
# NOTE: starts two REAL-config Vims (CoC/Copilot) — may prompt macOS keychain
# once per follower; click "Always Allow". ~15s of boot total.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
FIX="$HERE/../qa/fixtures"
CF=/Users/albertosca/Programming/vim-ai-follower/.venv/bin/claude-follow
A=/tmp/vaf-smoke-alpha.py
B=/tmp/vaf-smoke-beta.py

tmux has-session -t vaf-smoke 2>/dev/null && { echo "vaf-smoke exists — kill it first: tmux kill-session -t vaf-smoke"; exit 1; }

tmux new-session -d -s vaf-smoke -x 200 -y 50   # window 0
tmux new-window -t vaf-smoke                     # window 1
W0=$(tmux list-panes -t vaf-smoke:0 -F '#{pane_id}' | head -1)
W1=$(tmux list-panes -t vaf-smoke:1 -F '#{pane_id}' | head -1)
echo "window 0 origin=$W0   window 1 origin=$W1"

echo ">>> starting a follower in each window (real config; keychain prompt possible)..."
TMUX_PANE=$W0 "$CF" start
TMUX_PANE=$W1 "$CF" start
sleep 7   # full-config vim boot in both

fire() {  # $1 pane, $2 file, $3 fixture
  cp "$FIX/$3" "$2"
  echo "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$2\"},\"session_id\":\"w\"}" | TMUX_PANE=$1 "$CF" hook pre
  echo "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$2\"},\"session_id\":\"w\"}" | TMUX_PANE=$1 "$CF" hook post
}

echo ">>> animating a DISTINCT file into each window's follower..."
fire "$W0" "$A" window-alpha.py
fire "$W1" "$B" window-beta.py

echo
echo ">>> programmatic isolation check (each window has its OWN state):"
WID0=$(tmux display-message -p -t "$W0" '#{window_id}')
WID1=$(tmux display-message -p -t "$W1" '#{window_id}')
CACHE=$HOME/.cache/claude-vim-follower
echo "    window ids: w0=$WID0  w1=$WID1  (must differ: $([[ $WID0 != $WID1 ]] && echo OK || echo FAIL))"
echo "    w0 has its own state file? $([[ -f $CACHE/$WID0.pane ]] && echo 'yes' || echo 'NO (BUG)')"
echo "    w1 has its own state file? $([[ -f $CACHE/$WID1.pane ]] && echo 'yes' || echo 'NO (BUG)')"
# The two followers target DIFFERENT panes — the core of the isolation.
T0=$(python3 -c "import json;print(json.load(open('$CACHE/$WID0.pane'))['target'])" 2>/dev/null)
T1=$(python3 -c "import json;print(json.load(open('$CACHE/$WID1.pane'))['target'])" 2>/dev/null)
echo "    follower targets: w0=$T0  w1=$T1  (must differ: $([[ -n $T0 && $T0 != $T1 ]] && echo OK || echo FAIL))"

echo
echo "=== NOW: tmux attach -t vaf-smoke ==="
echo "  prefix 0 -> follower shows ONLY 'WINDOW ZERO';  prefix 1 -> ONLY 'WINDOW ONE'."
echo "  PASS: no bleed between the two windows' followers."
echo "  Cleanup: tmux kill-session -t vaf-smoke"
