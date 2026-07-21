#!/bin/zsh
# TEST 5 — Interrupt / hand-over over RPC.
# Needs the same SLOW follower as t4.
source "${0:A:h}/_lib.sh" || exit 1
F=/tmp/vaf-nvim-t5.py

echo "TEST 5 — interrupt (hand-over)  (press 'prefix S' WHILE it types)"
echo "As it animates: press 'prefix S' once to interrupt."
echo "Driving a slow edit now..."
drive pause-trigger.py "$F" ',"session_id":"me"'
echo
echo "After interrupt, in the NVIM pane: the buffer becomes MODIFIABLE — type a"
echo "small change, then ':w' to save. That releases Claude's turn."
echo
echo "PASS: buffer was editable after 'prefix S'; your ':w' printed an interrupt"
echo "      notification (the turn released with YOUR version)."
echo "FAIL: buffer stayed locked (nomodifiable), or the turn never released."
echo
echo "Next: t6-desinterrupt.sh"
