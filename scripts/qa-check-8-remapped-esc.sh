#!/bin/zsh
# QA Check 8 — remapped-<Esc> insert-exit. Runs the hermetic proof
# (scripts/repro-remapped-esc.sh) as-is — self-contained, private tmux
# socket, own trap cleanup EXIT — printed live for Alberto to watch, per the
# runbook's "Setup — hermetic proof" section. It then also sets up the
# runbook's real-world variant (restart the follower slow, fire the
# pause-trigger fixture against Alberto's REAL vim config) and leaves it
# animating, since the runbook calls that out as a distinct, non-hermetic
# check Alberto may want to watch with his own completion plugin active.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
FIX="$REPO/qa/fixtures"
source "$HERE/qa-lib.sh"

RUN_ID=$(qa_run_id)
qa_snapshot_cache
echo "QA run id: $RUN_ID"

echo ">>> Check 8 — remapped-<Esc> insert-exit"
echo ">>> Hermetic proof (worst case, no popup luck needed):"
zsh "$HERE/repro-remapped-esc.sh"

echo
echo "LOOK AT (hermetic): the printed '== two Escapes only ==' / '== shipped-exit =='"
echo "blocks and final verdict line above."
echo

if [[ -z "$TMUX_PANE" ]]; then
  echo ">>> Skipping the real-world variant: not inside a tmux pane."
  echo "    Run this script from the pane where 'claude' normally runs to"
  echo "    also set up the real-config variant against your own completion plugin."
  exit 0
fi

CF="$REPO/bin/claude-follow"
F=/tmp/vaf-qa-esc.py

echo ">>> Real-world variant: restarting the follower slow, firing the pause fixture"
echo "    against your real vim config (CoC/vim-ai-autocomplete)..."
"$CF" stop >/dev/null 2>&1 || true
"$CF" start --speed lento
cp "$FIX/pause-trigger.py" "$F"
P="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$F\"},\"session_id\":\"me\"}"
echo "$P" | "$CF" hook pre
echo "$P" | "$CF" hook post &

echo
echo "LOOK AT (real-world variant): every line of the animated buffer, looking"
echo "specifically for a leaked opener or a stray ':Nd'-style command fragment"
echo "on every line where a popup was up. Final buffer must match"
echo "$FIX/pause-trigger.py exactly."
echo
echo "Cleanup when done:  rm -f $F"
