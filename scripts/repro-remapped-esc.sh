#!/bin/zsh
# Deterministic proof that the tmux/Vim backend's insert-exit survives a
# REMAPPED insert-mode <Esc> that stays in insert mode — the class of
# vim-ai-autocomplete's EscHandler (dismiss the AI ghost, return '' -> stay
# insert) and CoC's popup close. When such a mapping consumes BOTH Escapes,
# a plain two-Escape exit leaves Vim in insert, so the next line's opener
# (o/i) is typed as LITERAL text on every line (the "o before every line"
# garble, 2026-07-22). animate._EXIT_INSERT appends <C-\><C-n> after the two
# Escapes — Vim's built-in force-normal-mode, which ignores every insert-mode
# mapping — so the exit is guaranteed. The two Escapes still run first so
# CoC/vim-visual-multi settle their popup/hit-enter state (see
# repro-plugin-preamble.sh for the VM-safety half).
#
# Hermetic and deterministic: models the worst case, `inoremap <Esc> <Nop>`
# (Esc does nothing at all), and drives the exact shapes the animation emits.
#
# PASS: two Escapes alone leave ":2d" as literal text (bug); the shipped
# two-Escapes-plus-<C-\><C-n> exit deletes the line (fix).
set -e
unset TMUX
export TMUX_TMPDIR=$(mktemp -d /tmp/vaf-resc-XXXX)
trap 'tmux kill-server 2>/dev/null; rm -rf "$TMUX_TMPDIR"' EXIT
V='set nocompatible noloadplugins | inoremap <Esc> <Nop>'  # worst case: Esc never exits insert

probe() {  # $1 = two-esc | shipped-exit  -> prints the buffer
  tmux kill-session -t m 2>/dev/null || true
  printf 'LINE-ONE\nLINE-TWO\nLINE-THREE\n' > "$TMUX_TMPDIR/f"
  VIMINIT="$V" tmux new-session -d -s m -x 120 -y 30 "vim $TMUX_TMPDIR/f"
  local P=$(tmux list-panes -t m -F '#{pane_id}' | head -1); sleep 1.0
  tmux send-keys -t "$P" gg
  tmux send-keys -t "$P" o                    # open a line -> insert mode
  tmux send-keys -t "$P" -l -- 'typed'
  tmux send-keys -t "$P" Escape; tmux send-keys -t "$P" Escape
  [[ "$1" == shipped-exit ]] && { tmux send-keys -t "$P" 'C-\' 'C-n'; }
  sleep 0.1
  tmux send-keys -t "$P" -l -- ':2d'; tmux send-keys -t "$P" Enter; sleep 0.3
  tmux send-keys -t "$P" 'C-\' 'C-n'; sleep 0.1   # force normal for the capture itself
  tmux send-keys -t "$P" -l -- ':redir! > '"$TMUX_TMPDIR"'/b | silent %p | redir END'
  tmux send-keys -t "$P" Enter; sleep 0.3
  tr -d '\000' < "$TMUX_TMPDIR/b"; tmux kill-session -t m 2>/dev/null || true
}

TWO=$(probe two-esc)
SHIPPED=$(probe shipped-exit)
echo "== two Escapes only =="; print -r -- "$TWO" | grep -v '^[[:space:]]*$'
echo "== two Escapes + <C-\\><C-n> (shipped) =="; print -r -- "$SHIPPED" | grep -v '^[[:space:]]*$'

two_has_cmd=$(print -r -- "$TWO" | grep -c ':2d' || true)
shipped_has_cmd=$(print -r -- "$SHIPPED" | grep -c ':2d' || true)
if [[ "$two_has_cmd" -ge 1 && "$shipped_has_cmd" -eq 0 ]]; then
  echo "PASS: two-Escape left ':2d' literal (bug); <C-\\><C-n> exit executed it (fix)"
else
  echo "INCONCLUSIVE: two=$two_has_cmd shipped=$shipped_has_cmd (vim/tmux behavior changed?)"
  exit 2
fi
