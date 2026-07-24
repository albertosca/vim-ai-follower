#!/bin/zsh
# QA Check 6 — nvim backend: pause / interrupt / des-interrupt. Restarts the
# nvim follower slow and fires the pause-trigger fixture over RPC, then
# leaves it animating so Alberto can exercise the three controls by hand
# (prefix P, prefix S, prefix S again) and inspect the buffer at each point.
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
F=/tmp/vaf-qa-nvim-pause.py

echo ">>> Check 6 — nvim backend: pause / interrupt / des-interrupt"
echo ">>> Restarting the nvim follower slow (--backend nvim --speed lento)..."
echo "    (stop on a launched nvim is a no-op by design — close a leftover"
echo "    Check 5 pane with :q first if it's cluttering)"
"$CF" stop >/dev/null 2>&1 || true
"$CF" start --backend nvim --speed lento

cp "$FIX/pause-trigger.py" "$F"
P="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$F\"},\"session_id\":\"me\"}"
echo "$P" | "$CF" hook pre
echo ">>> Firing hook post — animates slowly now."
echo "$P" | "$CF" hook post &

echo
echo "LOOK AT: the nvim buffer's state and modifiability at each control point:"
echo "  - Pause/resume (prefix P, then prefix P): typing halts at a clean line"
echo "    boundary, then resumes to the exact full content."
echo "  - Interrupt (prefix S mid-animation): typing stops and the buffer"
echo "    becomes modifiable — edit it, then :w releases Claude's turn."
echo "  - Des-interrupt (prefix S again, after an interrupt): unsaved typing"
echo "    is discarded and the remaining animation replays to the exact final"
echo "    content — no dropped lines, even across consecutive blank lines."
echo
echo "Cleanup when done:  rm -f $F"
echo "Close the leftover nvim pane/split yourself with :q (not killed by stop)."
