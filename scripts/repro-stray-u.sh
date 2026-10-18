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
# keychain. Private tmux socket: never the user's server. The full suite runs
# it (tests/test_integration_repro_stray_u.py); CI's unit job does not.
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
  # Each probe its own file and no swap (-n): a shared file left the first
  # Vim's swap behind, the second Vim opened at the ATTENTION prompt, and
  # the "double" verdict read empty whatever was typed (measured 2026-10-17:
  # it passed with the second Escape removed).
  local dump="$TMUX_TMPDIR/dump-$2.txt"
  rm -f "$dump"
  tmux kill-session -t strayu 2>/dev/null || true
  VIMINIT="$VIMRC" tmux -f /dev/null new-session -d -s strayu -x 120 -y 30 "vim -n $TMUX_TMPDIR/scratch-$2"
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
  # The buffer itself, behind a sentinel: a dump that never came is "no
  # answer", never an empty buffer.
  tmux send-keys -t "$P" -l -- ":call writefile(['BEGIN'] + getline(1, '\$'), '$dump')"
  tmux send-keys -t "$P" Enter
  local waited=0
  while [[ ! -s "$dump" && $waited -lt 30 ]]; do sleep 0.1; waited=$((waited + 1)); done
  tmux kill-session -t strayu 2>/dev/null || true
  if [[ "$(head -1 "$dump" 2>/dev/null)" != BEGIN ]]; then
    print -r -- "NO-DUMP"
    return
  fi
  print -r -- "$(tail -n +2 "$dump" | tr -d '\n')"
}

SINGLE=$(probe "single" single)
DOUBLE=$(probe "double" double)
echo "single-Escape rollback -> buffer: [$SINGLE]  (expected the bug: contains 'u')"
echo "double-Escape rollback -> buffer: [$DOUBLE]  (expected the fix: empty)"

if [[ "$SINGLE" == NO-DUMP || "$DOUBLE" == NO-DUMP ]]; then
  echo "INCONCLUSIVE: a Vim never wrote its buffer out"
  exit 2
fi
if [[ "$SINGLE" != *u* ]]; then
  echo "INCONCLUSIVE: single-Escape did not reproduce the stray 'u' (vim/tmux behavior changed?)"
  exit 2
fi
if [[ -n "$DOUBLE" ]]; then
  echo "FAIL: double-Escape rollback did NOT undo cleanly (buffer not empty)"
  exit 1
fi
echo "PASS: stray 'u' reproduced with single Escape and fixed by double Escape"
