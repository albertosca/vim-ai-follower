#!/bin/zsh
# Deterministic proof of the mode-switch garble and its fix. A completion
# popup maps the first <Esc> to close the popup and STAY in insert mode
# (as CoC does). If the animation ends an insert with a SINGLE Escape, the
# follower is still in insert mode when the NEXT op runs, so a `:2d` delete is
# typed as LITERAL text (":2d" lands in the buffer, the line is NOT deleted).
# With TWO Escapes (animate._EXIT_INSERT, the shipped fix) the second Escape
# leaves insert mode and `:2d` executes.
#
# Hermetic and deterministic (no plugins, no keychain, private tmux socket) —
# it drives the exact keystroke shape the animation emits.
#
# PASS: single-Escape leaves ":2d" literal in the buffer AND double-Escape
# does not (line actually deleted).
set -e
unset TMUX
export TMUX_TMPDIR=$(mktemp -d /tmp/vaf-mode-XXXX)
trap 'tmux kill-server 2>/dev/null; rm -rf "$TMUX_TMPDIR"' EXIT
printf 'banana\nbandana\n' > "$TMUX_TMPDIR/dict.txt"
V='set nocompatible noloadplugins'
V="$V | set dictionary=$TMUX_TMPDIR/dict.txt complete=k completeopt=menu"
V="$V | inoremap <expr> <Esc> pumvisible() ? \"\\<C-e>\" : \"\\<Esc>\""

probe() {  # $1 = single|double  -> prints the buffer's second line
  tmux kill-session -t m 2>/dev/null || true
  printf 'LINE-ONE\nLINE-TWO\nLINE-THREE\n' > "$TMUX_TMPDIR/f"
  VIMINIT="$V" tmux new-session -d -s m -x 120 -y 30 "vim $TMUX_TMPDIR/f"
  local P=$(tmux list-panes -t m -F '#{pane_id}' | head -1)
  sleep 1.2
  tmux send-keys -t "$P" gg
  tmux send-keys -t "$P" o                 # open a line (insert mode)
  tmux send-keys -t "$P" -l -- 'ban'
  tmux send-keys -t "$P" C-n               # completion popup appears
  sleep 0.3
  tmux send-keys -t "$P" Escape            # insert-ending Escape (eaten if single)
  [[ "$1" == double ]] && tmux send-keys -t "$P" Escape
  tmux send-keys -t "$P" -l -- ':2d'       # next op: delete line 2 (needs normal mode)
  tmux send-keys -t "$P" Enter
  sleep 0.4
  tmux send-keys -t "$P" Escape; tmux send-keys -t "$P" Escape; sleep 0.2
  tmux send-keys -t "$P" -l -- ':redir! > '"$TMUX_TMPDIR"'/b | silent %p | redir END'
  tmux send-keys -t "$P" Enter; sleep 0.3
  tr -d '\000' < "$TMUX_TMPDIR/b"
  tmux kill-session -t m 2>/dev/null || true
}

SINGLE=$(probe single)
DOUBLE=$(probe double)
echo "== single Escape =="; print -r -- "$SINGLE" | grep -v '^[[:space:]]*$'
echo "== double Escape =="; print -r -- "$DOUBLE" | grep -v '^[[:space:]]*$'

single_has_cmd=$(print -r -- "$SINGLE" | grep -c ':2d' || true)
double_has_cmd=$(print -r -- "$DOUBLE" | grep -c ':2d' || true)
if [[ "$single_has_cmd" -ge 1 && "$double_has_cmd" -eq 0 ]]; then
  echo "PASS: single-Escape typed ':2d' as literal text (bug); double-Escape deleted the line (fix)"
else
  echo "INCONCLUSIVE: single=$single_has_cmd double=$double_has_cmd (vim/tmux behavior changed?)"
  exit 2
fi
