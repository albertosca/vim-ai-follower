#!/bin/zsh
# TEST 1 — Start the nvim follower (launch path).
# Overrides config with --backend nvim; adopt_existing defaults to false, so
# this LAUNCHES a dedicated nvim in a split. It does NOT edit your config.
source /Users/albertosca/Programming/vim-ai-follower/scripts/nvim-smoke/_lib.sh || exit 1

echo "TEST 1 — start a dedicated nvim follower"
"$CF" start --backend nvim
echo
echo "PASS: a dedicated NVIM pane opened beside you (nvim, not vim)."
echo "FAIL: an error, no pane, or a Vim pane."
echo
echo "Next: t2-writer1-anim.sh"
