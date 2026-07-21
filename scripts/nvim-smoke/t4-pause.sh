#!/bin/zsh
# TEST 4 — Pause / resume over RPC.
# The control tests (t4-t6) need a SLOW follower so you can catch the animation
# mid-flight. If yours isn't slow, restart it first:
#     claude-follow stop 2>/dev/null
#     :q  the old launched nvim pane (stop is a no-op for launched nvim)
#     claude-follow start --backend nvim --speed lento
source /Users/albertosca/Programming/vim-ai-follower/scripts/nvim-smoke/_lib.sh || exit 1
F=/tmp/vaf-nvim-t4.py

echo "TEST 4 — pause / resume  (press keys WHILE it types)"
echo "As it animates: press 'prefix P' to pause, then 'prefix P' again to resume."
echo "Driving a slow edit now..."
drive pause-trigger.py "$F" ',"session_id":"me"'
echo
echo "PASS: typing halted at a clean LINE boundary on pause, then resumed to the"
echo "      exact full content on the second press. Final buffer matches the file."
echo "FAIL: stopped mid-character, garbled resume, or lost/duplicated lines."
echo
echo "Next: t5-interrupt.sh"
