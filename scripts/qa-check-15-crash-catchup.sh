#!/bin/zsh
# QA Check 15 — the crash-fallback catch-up rebuilds from the PERSISTED
# partial (d1620ff, 7a470c1, 4362526), not from whatever the abandoned buffer
# happens to hold. Fully script-driven: Alberto only looks at the result.
#
# The mechanics, in the order the script performs them, because they are the
# whole check:
#   1. Animate a fresh file slowly.
#   2. Ask the follower to PAUSE, through the same `claude-follow pause` the
#      `prefix P` keybinding runs. animate._wait_while_paused writes the
#      remainder to <window_id>.pending_animation.json and blocks in its poll
#      loop — the crash fallback exists for exactly this window.
#   3. Wait for that pending file to appear. It is the readiness signal: a
#      blind sleep here would let the kill land before the remainder was ever
#      persisted, and the whole check would then measure nothing.
#   4. kill -9 the hook process. -9 on purpose: the wait loop's `finally`
#      discards the pending file on any orderly exit, so a catchable signal
#      would clean up the very state a hook timeout leaves behind.
#   5. Fire the NEXT edit on the same file. hooks._animate_edit finds the
#      pending remainder, rebuilds the buffer from its persisted partial and
#      fast-forwards it at pace 0, then animates the new diff on top.
#
# Usage:  zsh scripts/qa-check-15-crash-catchup.sh [nvim|tmux]   (default nvim)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
FIX="$REPO/qa/fixtures"
source "$HERE/qa-lib.sh"

BACKEND="${1:-nvim}"
if [[ "$BACKEND" != "nvim" && "$BACKEND" != "tmux" ]]; then
  echo "ERROR: backend must be 'nvim' (default) or 'tmux'."
  exit 1
fi

if [[ -z "$TMUX_PANE" ]]; then
  echo "ERROR: run this from inside your tmux pane."
  exit 1
fi

RUN_ID=$(qa_run_id)
qa_snapshot_cache
echo "QA run id: $RUN_ID"

CF="$REPO/bin/claude-follow"
F=/tmp/vaf-qa-catchup.py
P="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$F\"},\"session_id\":\"me\"}"

echo ">>> Check 15 — crash-fallback catch-up (backend: $BACKEND)"
echo ">>> Restarting the follower slow (--backend $BACKEND --speed lento)..."
"$CF" stop >/dev/null 2>&1 || true
"$CF" start --backend "$BACKEND" --speed lento

WINDOW_ID=$(qa_window_id)
CACHE=~/.cache/claude-vim-follower
PENDING="$CACHE/${WINDOW_ID}.pending_animation.json"
rm -f "$PENDING"

rm -f "$F"
# Order matters: `hook pre` snapshots the CURRENT (absent) file first.
echo "$P" | "$CF" hook pre
cp "$FIX/catchup-blanks.py" "$F"
echo ">>> [1/5] first edit — animating slowly..."
"$CF" hook post <<< "$P" &
HOOK_PID=$!
# bin/claude-follow execs python in place, so $! IS the hook process.

sleep 6   # let real content land on screen before pausing

echo ">>> [2/5] pausing it (same path as prefix P)..."
"$CF" pause

echo ">>> [3/5] waiting for the crash-fallback remainder to be persisted..."
for _ in $(seq 1 100); do
  [[ -f "$PENDING" ]] && break
  sleep 0.1
done
if [[ ! -f "$PENDING" ]]; then
  echo "ERROR: no pending animation file appeared at $PENDING — the pause did"
  echo "not land, so there is nothing for the catch-up to rebuild from and"
  echo "this run would measure nothing. Aborting instead of reporting a pass."
  kill -9 "$HOOK_PID" 2>/dev/null || true
  exit 1
fi
echo "    persisted: $PENDING"

echo ">>> [4/5] killing the hook (kill -9 $HOOK_PID) — simulating a hook timeout..."
kill -9 "$HOOK_PID" 2>/dev/null || true
wait "$HOOK_PID" 2>/dev/null || true
if [[ ! -f "$PENDING" ]]; then
  echo "ERROR: the pending file disappeared with the hook — it was not killed"
  echo "hard enough to leave the crash fallback behind. Aborting."
  exit 1
fi
echo "    remainder survived the kill, as a real hook timeout leaves it"

echo ">>> [5/5] firing the NEXT edit on the same file..."
echo "$P" | "$CF" hook pre
cp "$FIX/catchup-blanks-next.py" "$F"
echo "$P" | "$CF" hook post &

TARGET=$(qa_follower_target "$WINDOW_ID")

echo
echo "LOOK AT: the follower catching up and then animating the new edit."
echo "  - The buffer first JUMPS to the rest of the first version (the pace-0"
echo "    catch-up, rebuilt from the persisted partial), then the new edit"
echo "    types on top of it."
echo "  - When it settles, the buffer is exactly"
echo "    $FIX/catchup-blanks-next.py — ending in the marker CATCHUP OK."
echo "  - Nothing is duplicated and nothing is stranded: no repeated line at"
echo "    the seam where the kill happened, and the blank-line gaps (two"
echo "    blanks between defs, one triple gap) are exactly as in the fixture."
echo
echo "Prove it from the real buffer, not the rendered pane:"
if [[ "$BACKEND" == "nvim" ]]; then
  echo "  source $HERE/qa-lib.sh && qa_dump_nvim_buffer $TARGET $F | diff - $FIX/catchup-blanks-next.py"
else
  echo "  source $HERE/qa-lib.sh && qa_dump_vim_buffer $TARGET | diff - $FIX/catchup-blanks-next.py"
fi
echo
echo "Cleanup when done:  rm -f $F"
if [[ "$BACKEND" == "nvim" ]]; then
  echo "Close the leftover nvim pane/split with :q if stop left one behind."
fi
