#!/bin/zsh
# TEST 3 — The nvim floating writer cue (writer 2, a distinct identity).
# Must run AFTER t2 against the same follower: the cue fires only once 2+
# DISTINCT writers have touched this window. Writer 2 carries an agent_id +
# agent_type, so it's the second identity.
source "${0:A:h}/_lib.sh" || exit 1
F=/tmp/vaf-nvim-cue.py

echo "TEST 3 — writer 2 (code-reviewer): the floating status window"
echo "Watch the NVIM pane now..."
drive cue-writer2.py "$F" ',"session_id":"you-foreground","agent_id":"rev1","agent_type":"code-reviewer"'
echo
echo "PASS: a small FLOATING WINDOW appeared with the label 'code-reviewer' in a color,"
echo "      and a virtual-text label near the cursor while it typed."
echo "FAIL: no floating window, or no label / no color."
echo
echo "Next: t4-pause.sh"
