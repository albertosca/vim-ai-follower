#!/bin/zsh
# TEST 6 — Des-interrupt (second S) replay to exact content, across blank lines.
# Needs the same SLOW follower. Uses nvim-blanks.py (double blank lines between
# defs) so a des-interrupt landing near a gap exercises the 27aa0e4 fix.
source /Users/albertosca/Programming/vim-ai-follower/scripts/nvim-smoke/_lib.sh || exit 1
F=/tmp/vaf-nvim-t6.py

echo "TEST 6 — des-interrupt (discard your typing, replay the remainder)"
echo "As it animates: press 'prefix S' once (interrupt), THEN 'prefix S' again"
echo "(des-interrupt). Try to interrupt around one of the double blank-line gaps."
echo "Driving a slow edit now..."
drive nvim-blanks.py "$F" ',"session_id":"me"'
echo
echo "PASS: after the 2nd 'prefix S', your unsaved typing is discarded and the"
echo "      REMAINING animation replays to the EXACT final content — no dropped"
echo "      line, no stray blank, no flash of the finished file."
echo "FAIL: a dropped/added line (watch the blank-line gaps), or the finished"
echo "      file flashed in whole instead of replaying."
echo
echo "Eyeball the nvim buffer against the file on disk (the source of truth):"
echo "    cat $F        # what the follower buffer must end up matching, line for line"
echo
echo "Next: t7-cleanup.sh"
