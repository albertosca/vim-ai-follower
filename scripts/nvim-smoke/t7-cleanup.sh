#!/bin/zsh
# TEST 7 — Cleanup.
source /Users/albertosca/Programming/vim-ai-follower/scripts/nvim-smoke/_lib.sh || exit 1

echo "TEST 7 — cleanup"
"$CF" stop 2>/dev/null
rm -f /tmp/vaf-nvim-*.py
echo "State + keybindings cleared, scratch files removed."
echo
echo "NOTE: a LAUNCHED nvim follower is NOT killed by stop (by design — it never"
echo "kills your editor). Close each leftover nvim split yourself with :q."
