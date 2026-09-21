#!/bin/zsh
# QA Check 18 — nvim backend: a file whose swap a LIVE editor holds opens,
# instead of crashing the hook on E325 (d817f08). Script-driven end to end.
#
# Unlike the tmux backend there is no stall here: nvim raises Vim:E325 out of
# `bufload` straight into the RPC call, the channel stays responsive, and the
# HOOK PROCESS is what dies. So the observable is not a dialog on screen — it
# is a traceback and a non-zero exit from `claude-follow hook post`, with the
# file never shown. This script runs that hook in the FOREGROUND and prints
# both, so a pass and a failure cannot look the same.
#
# The swap owner is a second real NVIM, not a Vim: nvim's default `directory`
# is ~/.local/state/nvim/swap//, while Vim's starts with '.', so a Vim-made
# swap beside the file in /tmp is not where the follower's nvim looks and the
# check would be vacuous.
#
# Note also why the automated suite could not catch this: the shared
# `headless_nvim` fixture launches with `-n`, which suppresses the swap check
# outright. This manual check uses the follower's own launched nvim, which has
# no `-n`, so the check is real.
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
RUN_DIR="/tmp/vaf-qa-${RUN_ID}"
mkdir -p "$RUN_DIR"
qa_snapshot_cache
echo "QA run id: $RUN_ID (scratch: $RUN_DIR)"

CF="$REPO/bin/claude-follow"
F=/tmp/vaf-qa-nvim-swap.py

echo ">>> Check 18 — nvim: a swap-held file opens instead of crashing the hook"
echo ">>> Restarting the nvim follower (--backend nvim)..."
"$CF" stop >/dev/null 2>&1 || true
"$CF" start --backend nvim

cp "$FIX/standalone-demo.py" "$F"

echo ">>> Opening a second, real nvim on $F in its own pane (the swap owner)..."
OWNER=$(tmux split-window -P -F '#{pane_id}' -h -t "$TMUX_PANE" nvim "$F")
SWAPDIR=~/.local/state/nvim/swap
for _ in $(seq 1 100); do
  [[ -n "$(find "$SWAPDIR" -name '*vaf-qa-nvim-swap*' 2>/dev/null)" ]] && break
  sleep 0.1
done
FOUND=$(find "$SWAPDIR" -name '*vaf-qa-nvim-swap*' 2>/dev/null)
if [[ -z "$FOUND" ]]; then
  echo "ERROR: no swap file for $F under $SWAPDIR. Without one there is no"
  echo "E325 to survive and this run would measure nothing. (Check the other"
  echo "nvim's :set directory? — a config that disables swap files makes this"
  echo "check impossible.)"
  tmux kill-pane -t "$OWNER" 2>/dev/null || true
  exit 1
fi
echo "    swap present: $FOUND"

echo ">>> Read-navigating the follower to it, hook in the FOREGROUND..."
R="{\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"$F\"},\"session_id\":\"me\"}"
set +e
echo "$R" | "$CF" hook post > "$RUN_DIR/hook.out" 2> "$RUN_DIR/hook.err"
HOOK_CODE=$?
set -e
echo "    hook post exit code: $HOOK_CODE   (want 0)"
if [[ -s "$RUN_DIR/hook.err" ]]; then
  echo "    !! the hook wrote to stderr — this is the crash this check guards:"
  sed 's/^/    | /' "$RUN_DIR/hook.err"
else
  echo "    hook stderr: empty (no traceback)"
fi

WINDOW_ID=$(qa_window_id)
SOCK=$(qa_follower_target "$WINDOW_ID")

echo
echo "LOOK AT: the follower nvim pane."
echo "  - The file's real content is shown (the bfs() algorithm), in its own"
echo "    tab. Not an empty buffer, and not nothing at all."
echo "  - The line above must read 'exit code: 0' with an empty stderr. A"
echo "    non-zero code with a Vim:E325 traceback is the FAIL."
echo "  - The other nvim (pane $OWNER) is untouched and still usable."
echo "  - Nothing alarming in the hook log:"
echo "        grep -i -e traceback -e error -e E325 ~/.cache/claude-vim-follower/hook.log"
echo
echo "Prove the content from the real buffer, not the rendered pane:"
echo "  source $HERE/qa-lib.sh && qa_dump_nvim_buffer $SOCK $F | diff - $FIX/standalone-demo.py"
echo
echo "Cleanup when done:"
echo "  tmux kill-pane -t $OWNER        # close the other nvim"
echo "  rm -f $F"
echo "  rm -rf $RUN_DIR"
echo "  find $SWAPDIR -name '*vaf-qa-nvim-swap*' -delete"
echo "Close the leftover follower nvim pane with :q if stop left one behind."
