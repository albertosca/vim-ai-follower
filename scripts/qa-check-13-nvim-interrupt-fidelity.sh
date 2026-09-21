#!/bin/zsh
# QA Check 13 — nvim backend: what an interrupt LEAVES BEHIND. Two steps, run
# one after the other against the same follower and the same file:
#
#   line  (default)  a fresh retype. `prefix S` mid-line must leave the line
#                    cut exactly where it was typed, NOT snapped to its full
#                    text (1d2b5a9); `prefix S` again retypes that line from
#                    its start and finishes the file.
#   op               an edit whose script is one `replace` of 1 line -> 3.
#                    `prefix S` inside it must put the deleted line BACK, so
#                    the buffer equals exactly what was on screen before the
#                    op started (6bef225); `prefix S` again replays the op
#                    from its start and lands on the exact final content.
#
# Usage:  zsh scripts/qa-check-13-nvim-interrupt-fidelity.sh [line|op]
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
FIX="$REPO/qa/fixtures"
source "$HERE/qa-lib.sh"

STEP="${1:-line}"
if [[ "$STEP" != "line" && "$STEP" != "op" ]]; then
  echo "ERROR: step must be 'line' (default) or 'op'."
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
F=/tmp/vaf-qa-rollback.py
P="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$F\"},\"session_id\":\"me\"}"

if [[ "$STEP" == "line" ]]; then
  echo ">>> Check 13 step 'line' — nvim interrupt leaves the half-typed line"
  echo ">>> Restarting the nvim follower slow (--backend nvim --speed lento)..."
  "$CF" stop >/dev/null 2>&1 || true
  "$CF" start --backend nvim --speed lento
  rm -f "$F"
  # Order matters: `hook pre` snapshots the CURRENT (absent) file first.
  echo "$P" | "$CF" hook pre
  cp "$FIX/rollback-before.py" "$F"
  echo ">>> Firing hook post — fresh retype, animating slowly now."
  echo "$P" | "$CF" hook post &
else
  echo ">>> Check 13 step 'op' — an interrupted op rolls its delete back"
  echo ">>> (reuses the follower and the file step 'line' left behind)"
  if [[ ! -f "$F" ]]; then
    echo "ERROR: $F does not exist — run step 'line' first, and let it finish."
    exit 1
  fi
  # `hook pre` must snapshot rollback-before.py, THEN the after-content lands.
  echo "$P" | "$CF" hook pre
  cp "$FIX/rollback-after.py" "$F"
  echo ">>> Firing hook post — one replace op, 1 line -> 3, animating slowly."
  echo "$P" | "$CF" hook post &
fi

WINDOW_ID=$(qa_window_id)
SOCK=$(qa_follower_target "$WINDOW_ID")

echo
if [[ "$STEP" == "line" ]]; then
  echo "LOOK AT: press prefix S while a LINE is half typed — mid-word is the"
  echo "point, do not wait for a line boundary."
  echo "  - After the first S: the line stays CUT exactly where the cursor"
  echo "    was, the rest of it simply absent (it must NOT silently complete"
  echo "    itself first), and the buffer becomes modifiable."
  echo "  - After the second S (des-interrupt): that same line is retyped FROM"
  echo "    ITS START — not resumed mid-word — and the rest of the file"
  echo "    follows, landing on exactly $FIX/rollback-before.py."
  echo
  echo "Prove the content from the real buffer, not the rendered pane:"
  echo "  source $HERE/qa-lib.sh && qa_dump_nvim_buffer $SOCK $F | diff - $FIX/rollback-before.py"
  echo
  echo "Next step, once this one has a verdict AND the animation finished:"
  echo "  zsh $HERE/$(basename "$0") op"
else
  echo "LOOK AT: press prefix S while the THREE new lines are typing — wait"
  echo "until at least the first of them is partly on screen, because an S"
  echo "landing before the op's delete correctly touches nothing at all."
  echo "  - After the first S: total_of's ORIGINAL single 'return sum(...)'"
  echo "    line is BACK. The buffer is exactly the before-version again — no"
  echo "    half-typed leftover, no blank rows where the three new lines were"
  echo "    going, nothing missing."
  echo "  - After the second S (des-interrupt): the op replays from its start"
  echo "    and the buffer lands on exactly $FIX/rollback-after.py, with no"
  echo "    duplicated or missing line around the replaced region."
  echo
  echo "Prove each state from the real buffer, not the rendered pane:"
  echo "  source $HERE/qa-lib.sh && qa_dump_nvim_buffer $SOCK $F | diff - $FIX/rollback-before.py   # after the 1st S"
  echo "  source $HERE/qa-lib.sh && qa_dump_nvim_buffer $SOCK $F | diff - $FIX/rollback-after.py    # after the 2nd S"
  echo
  echo "Cleanup when done:  rm -f $F"
  echo "Close the leftover nvim pane/split with :q if stop left one behind."
fi
