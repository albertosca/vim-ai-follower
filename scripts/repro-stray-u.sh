#!/bin/zsh
# Regression proof for the stray-"u" on a mid-animation interrupt rollback.
#
# Root cause: on an interrupt/pause landing mid-line, animate.py rolls the
# half-typed line back with Escape then "u" (undo). A completion popup
# (CoC/Copilot commonly map `inoremap <expr> <Esc> pumvisible() ? "\<C-e>"
# : "\<Esc>"`) makes the FIRST Escape close the popup but STAY in insert
# mode, so the "u" is typed literally -> "banu". The fix (animate.py
# _exit_insert_mode) sends TWO Escapes: whatever eats the first, the second
# leaves insert mode, so "u" undoes. Same guarantee as _normal_mode.
#
# This is hermetic and deterministic — it simulates the popup-close mapping
# and opens a real built-in completion popup, so it needs no plugins and no
# keychain, and can run in CI. Private tmux socket: never the user's server.
#
# PASS: single-Escape reproduces the stray "u" AND double-Escape is clean.
set -e
unset TMUX
export TMUX_TMPDIR=$(mktemp -d /tmp/vaf-strayu-XXXX)
trap 'tmux kill-server 2>/dev/null; rm -rf "$TMUX_TMPDIR"' EXIT

printf 'banana\nbandana\n' > "$TMUX_TMPDIR/dict.txt"
VIMRC='set nocompatible noloadplugins'
VIMRC="$VIMRC | set dictionary=$TMUX_TMPDIR/dict.txt complete=k completeopt=menu"
VIMRC="$VIMRC | inoremap <expr> <Esc> pumvisible() ? \"\\<C-e>\" : \"\\<Esc>\""

probe() {  # $1 label, $2 = single|double
  tmux kill-session -t strayu 2>/dev/null || true
  VIMINIT="$VIMRC" tmux new-session -d -s strayu -x 120 -y 30 "vim $TMUX_TMPDIR/scratch"
  local P=$(tmux list-panes -t strayu -F '#{pane_id}' | head -1)
  sleep 1.2
  tmux send-keys -t "$P" i
  tmux send-keys -t "$P" -l -- 'ban'
  tmux send-keys -t "$P" C-n            # open the completion popup
  sleep 0.4
  tmux send-keys -t "$P" Escape
  [[ "$2" == double ]] && tmux send-keys -t "$P" Escape
  tmux send-keys -t "$P" -l -- 'u'      # the undo the rollback sends next
  sleep 0.5
  tmux send-keys -t "$P" Escape; tmux send-keys -t "$P" Escape; sleep 0.2
  tmux send-keys -t "$P" -l -- ':redir! > '"$TMUX_TMPDIR"'/b.txt | silent %p | redir END'
  tmux send-keys -t "$P" Enter; sleep 0.3
  local buf=$(tr -d '\000' < "$TMUX_TMPDIR/b.txt" | sed 's/^[[:space:]]*[0-9]*//;/^[[:space:]]*$/d' | tr -d '[:space:]')
  tmux kill-session -t strayu 2>/dev/null || true
  print -r -- "$buf"
}

SINGLE=$(probe "single" single)
DOUBLE=$(probe "double" double)
echo "single-Escape rollback -> buffer: [$SINGLE]  (expected the bug: contains 'u')"
echo "double-Escape rollback -> buffer: [$DOUBLE]  (expected the fix: empty)"

if [[ "$SINGLE" != *u* ]]; then
  echo "INCONCLUSIVE: single-Escape did not reproduce the stray 'u' (vim/tmux behavior changed?)"
  exit 2
fi
if [[ -n "$DOUBLE" ]]; then
  echo "FAIL: double-Escape rollback did NOT undo cleanly (buffer not empty)"
  exit 1
fi
echo "PASS: stray 'u' reproduced with single Escape and fixed by double Escape"
