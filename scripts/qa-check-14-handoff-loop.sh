#!/bin/zsh
# QA Check 14 — the interrupt / des-interrupt hand-off CYCLES. Before 5b87c34
# the hand-off wait ended at the first replay stop: S (interrupt), S
# (des-interrupt), S mid-replay (interrupt again) worked, and the FOURTH press
# did nothing at all — nobody was listening any more, and the buffer stayed
# partial and modifiable until some later animation's relock healed it. The
# wait is now a loop, one iteration per cycle, with no ceiling.
#
# Backend-shared: the loop lives in hooks._await_user_handoff, above both
# backends, so this runs on either one.
#
# Usage:  zsh scripts/qa-check-14-handoff-loop.sh [nvim|tmux]   (default nvim)
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
F=/tmp/vaf-qa-handoff-loop.py

echo ">>> Check 14 — interrupt / des-interrupt loop (backend: $BACKEND)"
echo ">>> Restarting the follower slow (--backend $BACKEND --speed lento)..."
"$CF" stop >/dev/null 2>&1 || true
"$CF" start --backend "$BACKEND" --speed lento

rm -f "$F"
P="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$F\"},\"session_id\":\"me\"}"
# Order matters: `hook pre` snapshots the CURRENT (absent) file first.
echo "$P" | "$CF" hook pre
cp "$FIX/nvim-blanks.py" "$F"
echo ">>> Firing hook post — animates slowly now."
echo "$P" | "$CF" hook post &

WINDOW_ID=$(qa_window_id)
TARGET=$(qa_follower_target "$WINDOW_ID")

echo
echo "LOOK AT: press prefix S over and over, alternating interrupt and"
echo "des-interrupt, AT LEAST FOUR TIMES — the fourth press is the one that"
echo "used to do nothing:"
echo "  1st S  interrupt    -> typing stops, the buffer becomes modifiable"
echo "  2nd S  des-interrupt-> the remaining animation replays"
echo "  3rd S  interrupt    -> the replay stops, buffer modifiable again"
echo "  4th S  des-interrupt-> the replay RESUMES (this is the fix)"
echo "  ...keep going for as many cycles as you like; every press must land."
echo
echo "  - Every press gets a visible response. A press that does nothing at"
echo "    all is the FAIL this check exists for."
echo "  - Each des-interrupt discards whatever you typed and picks up from"
echo "    the same place, never from the top and never skipping content."
echo "  - Let the last des-interrupt run to the end: the buffer must equal"
echo "    $FIX/nvim-blanks.py exactly, with the consecutive blank lines"
echo "    intact (no dropped and no doubled blank)."
echo
echo "You can watch the state machine from another pane while you press:"
echo "  cat ~/.cache/claude-vim-follower/${WINDOW_ID}.animating"
echo "  (prints '<pid> running' / '<pid> paused' / '<pid> handoff')"
echo
echo "Prove the final content from the real buffer, not the rendered pane:"
if [[ "$BACKEND" == "nvim" ]]; then
  echo "  source $HERE/qa-lib.sh && qa_dump_nvim_buffer $TARGET $F | diff - $FIX/nvim-blanks.py"
else
  echo "  source $HERE/qa-lib.sh && qa_dump_vim_buffer $TARGET | diff - $FIX/nvim-blanks.py"
fi
echo
echo "Cleanup when done:  rm -f $F"
if [[ "$BACKEND" == "nvim" ]]; then
  echo "Close the leftover nvim pane/split with :q if stop left one behind."
fi
