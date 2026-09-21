#!/bin/zsh
# QA Check 17 — tmux backend: a swap file on the target is answered
# "(E)dit anyway" instead of stalling the pane (e0bb5be). Two steps:
#
#   live  (default)  a second, LIVE Vim in its own pane holds the file's
#                    swap — the everyday adopt-mode shape: the user's own
#                    editor on the file Claude is writing. The follower must
#                    navigate to it with no dialog, and that other Vim must
#                    still be able to `:w` its work afterwards.
#   stale            a swap left behind by a crashed Vim (the script makes
#                    one by kill -9'ing a Vim that had the file open). The
#                    follower must navigate with no dialog AND leave the
#                    swap file on disk — the policy is Edit, never
#                    "(D)elete it", so no recovery data is destroyed.
#
# GEOMETRY IS LOAD-BEARING, same as Check 16: at 49 columns the ATTENTION
# block is long enough to hit the `-- More --` pager BEFORE it reaches the
# `[O]pen Read-Only, (E)dit anyway, ...` question, which is what makes the
# old failure a double stall. Wide panes make this check vacuous, so the
# follower pane is narrowed and the width is printed.
#
# Usage:  zsh scripts/qa-check-17-tmux-swap.sh [live|stale]   (default live)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
FIX="$REPO/qa/fixtures"
source "$HERE/qa-lib.sh"

STEP="${1:-live}"
if [[ "$STEP" != "live" && "$STEP" != "stale" ]]; then
  echo "ERROR: step must be 'live' (default) or 'stale'."
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
F="/tmp/vaf-qa-swap-${STEP}.py"
SWAP="/tmp/.$(basename "$F").swp"

echo ">>> Check 17 step '$STEP' — tmux: swap-file ATTENTION answered, not stalled"
if [[ "$STEP" == "live" ]]; then
  echo ">>> Restarting the tmux follower (--backend tmux)..."
  "$CF" stop >/dev/null 2>&1 || true
  "$CF" start --backend tmux
fi

WINDOW_ID=$(qa_window_id)
FOLLOWER=$(qa_follower_target "$WINDOW_ID")
if [[ -z "$FOLLOWER" ]]; then
  echo "ERROR: no follower registered for $WINDOW_ID — run step 'live' first."
  exit 1
fi

cp "$FIX/standalone-demo.py" "$F"
rm -f "$SWAP"

# The swap owner goes in its own pane FIRST, then the follower is narrowed:
# creating a pane afterwards would resize it again and undo this.
echo ">>> Opening a second, real Vim on $F in its own pane..."
OWNER=$(tmux split-window -P -F '#{pane_id}' -h -t "$TMUX_PANE" vim "$F")
for _ in $(seq 1 100); do
  [[ -f "$SWAP" ]] && break
  sleep 0.1
done
if [[ ! -f "$SWAP" ]]; then
  echo "ERROR: no swap file appeared at $SWAP. Without one there is no"
  echo "ATTENTION dialog to answer and this run would measure nothing."
  echo "(Vim's 'directory' option decides where the swap lands; the default"
  echo "starts with '.', which is what puts it beside the file in /tmp.)"
  tmux kill-pane -t "$OWNER" 2>/dev/null || true
  exit 1
fi
echo "    swap present: $SWAP"

if [[ "$STEP" == "stale" ]]; then
  echo ">>> Killing that Vim hard, so the swap it leaves behind is STALE..."
  OWNER_PID=$(tmux display-message -p -t "$OWNER" '#{pane_pid}')
  kill -9 "$OWNER_PID" 2>/dev/null || true
  sleep 1
  tmux kill-pane -t "$OWNER" 2>/dev/null || true
  OWNER=""
  if [[ ! -f "$SWAP" ]]; then
    echo "ERROR: the swap file vanished with the Vim — nothing stale is left"
    echo "to trip the dialog. Aborting instead of reporting a pass."
    exit 1
  fi
  echo "    stale swap still on disk: $SWAP"
fi

WAS_WIDTH=$(tmux display-message -p -t "$FOLLOWER" '#{pane_width}')
tmux resize-pane -t "$FOLLOWER" -x 49 2>/dev/null || true
NOW_WIDTH=$(tmux display-message -p -t "$FOLLOWER" '#{pane_width}')
echo ">>> follower pane $FOLLOWER: width $WAS_WIDTH -> $NOW_WIDTH"
if [[ "$NOW_WIDTH" -ge 51 ]]; then
  echo "WARNING: could not narrow the follower below 51 columns. The"
  echo "ATTENTION block may not reach the pager at this width, so a clean"
  echo "pane below proves less than it looks like. Use a wider terminal"
  echo "window (so the split can be narrower) and re-run."
fi

echo ">>> Navigating the follower to the swap-held file (Read hook)..."
R="{\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"$F\"},\"session_id\":\"me\"}"
echo "$R" | "$CF" hook post
sleep 1

echo
echo "LOOK AT: the FOLLOWER pane ($FOLLOWER), right now."
echo "  - It shows the real content of $F (the bfs() algorithm)."
echo "  - NO 'E325', no 'ATTENTION', no 'Swap file ... already exists', no"
echo "    '[O]pen Read-Only, (E)dit anyway' question, no '-- More --' pager,"
echo "    no 'Press ENTER'."
echo "  - Prove it is live with a NON-colon keystroke: press j or G in that"
echo "    pane and watch the cursor move. A ':' command is not a valid probe"
echo "    — ':' is a real key in More mode and would dismiss the very prompt"
echo "    it was sent to detect."
if [[ "$STEP" == "live" ]]; then
  echo "  - Then go to the OTHER Vim (pane $OWNER), type something, and :w —"
  echo "    it must save normally. The follower answering the dialog must not"
  echo "    have cost the user's own editor anything."
else
  echo "  - The stale swap must STILL be on disk afterwards:"
  echo "        ls -l $SWAP"
  echo "    The policy is (E)dit anyway, never (D)elete it — no recovery data"
  echo "    is thrown away."
fi
echo
echo "Cleanup when done:"
if [[ "$STEP" == "live" ]]; then
  echo "  (in pane $OWNER) :q!            # close the other Vim"
  echo "  tmux kill-pane -t $OWNER        # or just kill its pane"
  echo "  zsh $HERE/$(basename "$0") stale   # next step, before cleaning up"
fi
echo "  rm -f $F $SWAP"
echo "  tmux resize-pane -t $FOLLOWER -x $WAS_WIDTH   # restore the pane width"
