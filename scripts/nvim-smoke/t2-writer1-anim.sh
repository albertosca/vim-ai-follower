#!/bin/zsh
# TEST 2 — Char-by-char animation (writer 1, no floating cue yet).
source "${0:A:h}/_lib.sh" || exit 1
F=/tmp/vaf-nvim-cue.py

echo "TEST 2 — writer 1 types in char-by-char"
echo "Watch the NVIM pane now..."
drive cue-writer1.py "$F" ',"session_id":"you-foreground"'
echo
echo "PASS: content typed in CHAR-BY-CHAR (current line highlighted, cursor following)."
echo "FAIL: content flashed in whole, or no highlight / no cursor motion."
echo
echo "Next: t3-writer2-floating.sh  (run it against the SAME follower)"
