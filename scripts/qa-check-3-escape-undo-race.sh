#!/bin/zsh
# QA Check 3 — Escape+undo race fix. Restarts the follower slow, fires the
# pause-trigger fixture (real os./ .strip()/ sorted( completions), and leaves
# it animating so Alberto can pause mid-line (prefix P) while a completion
# popup is visible, then resume and inspect the final buffer.
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
F=/tmp/vaf-qa-pause.py

echo ">>> Check 3 — Escape+undo race fix"
echo ">>> Restarting the follower slow (--speed lento)..."
"$CF" stop >/dev/null 2>&1 || true
"$CF" start --speed lento

cp "$FIX/pause-trigger.py" "$F"
P="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$F\"},\"session_id\":\"me\"}"
echo "$P" | "$CF" hook pre
echo ">>> Firing hook post — animates slowly now. Get ready to press prefix P."
echo "$P" | "$CF" hook post &

echo
echo "LOOK AT: while a line with 'os.' / '.strip()' / 'sorted(' is animating and"
echo "a completion popup (CoC/Copilot/vim-ai-autocomplete) is visible, press"
echo "prefix P to pause, then prefix P again to resume (or let it finish)."
echo "Then inspect the FINAL buffer content line by line — it must match"
echo "$FIX/pause-trigger.py exactly, with no stray 'u' or 'banu'-style smear."
echo
echo "Hermetic proof of the same fix (no popup timing luck needed):"
echo "  zsh $HERE/repro-stray-u.sh"
echo
echo "Cleanup when done:  rm -f $F"
