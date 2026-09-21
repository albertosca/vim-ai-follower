#!/bin/zsh
# QA Check 16 — tmux backend: navigating to a DIRTY buffer must not stall the
# follower pane on Vim's E37 hit-enter prompt (511ceb6). Script-driven end to
# end; Alberto only reads the pane.
#
# `:tab drop {file}` ends with a `:rewind` whose abandon check raises
# `E37: No write since last change (add ! to override)` when the buffer it
# landed on is modified — the ordinary state after an interrupt hand-off or a
# killed hook. goto_file now swallows exactly that error.
#
# GEOMETRY IS LOAD-BEARING and this script enforces it. E37 is 51 characters;
# it only WRAPS (and a wrapped message is what makes Vim escalate to a real
# blocking "Press ENTER" prompt) in a pane narrower than that. Run against a
# wide follower pane, the whole check passes vacuously — nothing to see either
# way. So the pane is resized to 49 columns first, and the width is printed.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
FIX="$REPO/qa/fixtures"
source "$HERE/qa-lib.sh"

if [[ -z "$TMUX_PANE" ]]; then
  echo "ERROR: run this from inside your tmux pane."
  exit 1
fi

RUN_ID=$(qa_run_id)
qa_snapshot_cache
echo "QA run id: $RUN_ID"

CF="$REPO/bin/claude-follow"
F=/tmp/vaf-qa-dirty-nav.py

echo ">>> Check 16 — tmux: no E37 hit-enter prompt on a dirty target"
echo ">>> Restarting the tmux follower slow (--backend tmux --speed lento)..."
"$CF" stop >/dev/null 2>&1 || true
"$CF" start --backend tmux --speed lento

WINDOW_ID=$(qa_window_id)
FOLLOWER=$(qa_follower_target "$WINDOW_ID")
WAS_WIDTH=$(tmux display-message -p -t "$FOLLOWER" '#{pane_width}')
tmux resize-pane -t "$FOLLOWER" -x 49 2>/dev/null || true
NOW_WIDTH=$(tmux display-message -p -t "$FOLLOWER" '#{pane_width}')
echo ">>> follower pane $FOLLOWER: width $WAS_WIDTH -> $NOW_WIDTH (E37 is 51 chars;"
echo "    it must WRAP for the old bug to be visible at all)"
if [[ "$NOW_WIDTH" -ge 51 ]]; then
  echo "WARNING: the pane could not be narrowed below 51 columns. At this"
  echo "width E37 does not wrap and Vim never escalates to a hit-enter"
  echo "prompt, so a clean pane below proves NOTHING. Widen your terminal"
  echo "window is the wrong direction — make it WIDER overall so the split"
  echo "can be narrowed, or run this check in a window at least 100 columns"
  echo "across, and re-run."
fi

rm -f "$F"
P="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$F\"},\"session_id\":\"me\"}"
echo "$P" | "$CF" hook pre
cp "$FIX/pause-trigger.py" "$F"
echo ">>> [1/4] animating the file..."
"$CF" hook post <<< "$P" &
HOOK_PID=$!
# bin/claude-follow execs python in place, so $! IS the hook process.
sleep 5

echo ">>> [2/4] interrupting it — a hand-off leaves the buffer MODIFIED and"
echo "    unlocked, which is exactly the dirty target E37 used to fire on..."
"$CF" interrupt
sleep 2

echo ">>> [3/4] killing the waiting hook, so the window is free for the next"
echo "    one while the buffer stays dirty (a real hook timeout does this)..."
kill -9 "$HOOK_PID" 2>/dev/null || true
wait "$HOOK_PID" 2>/dev/null || true

echo ">>> [4/4] firing a Read navigation at that same dirty buffer..."
R="{\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"$F\"},\"session_id\":\"me\"}"
echo "$R" | "$CF" hook post
sleep 1

echo
echo "LOOK AT: the follower pane, right now, straight after that navigation."
echo "  - NO 'E37: No write since last change' anywhere."
echo "  - NO 'Press ENTER or type command to continue' prompt, and no"
echo "    '-- More --' pager: the pane is live, not waiting on you."
echo "  - The file is shown, and the half-typed content the interrupt left is"
echo "    still there — the navigation must not have reloaded it from disk."
echo "  - Prove it is really live with a NON-colon keystroke: press j or G in"
echo "    that pane and watch the cursor move. A ':' command is not a valid"
echo "    probe — ':' is a real key at a hit-enter prompt, so an Ex command"
echo "    would dismiss the very prompt it was sent to detect."
echo
echo "Cleanup when done:"
echo "  rm -f $F"
echo "  tmux resize-pane -t $FOLLOWER -x $WAS_WIDTH   # restore the pane width"
