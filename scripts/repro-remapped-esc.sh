#!/bin/zsh
# The hazard that motivated (and then mis-motivated) the insert-exit sequence.
#
# WHAT IS STABLE AND WHAT THIS SCRIPT ASSERTS: an ACTIVE insert-mode <Esc>
# mapping that stays in insert really does swallow both Escapes, so the next
# ex-command lands in the buffer as literal text. That is the hazard.
#
# WHAT THIS SCRIPT DELIBERATELY DOES NOT ASSERT: that the hazard reaches the
# product. It does not. animate._EXIT_INSERT is two Escapes and nothing else,
# and a full animation against the real config — which DOES remap insert <Esc>
# (vim_ai_autocomplete#EscHandler) — comes out byte-perfect; that is measured by
# scripts/repro-exit-insert-matrix.sh, which is the regression guard.
#
# HISTORY, worth keeping: this script used to conclude the opposite, that
# <C-\><C-n> had to be appended to guarantee the exit. It reached that
# conclusion with a probe that never set 'paste', while the follower always
# animates under `:setlocal modifiable paste`. The <C-\><C-n> added on that
# basis then corrupted the buffer on EVERY animation (two mechanisms, see the
# comment on animate._EXIT_INSERT). Whether 'paste' is what defuses the mapping
# is still open: probes of the synthetic worst case disagreed across runs, and
# several disagreements traced to harness artifacts (a startup race at ~1s of
# settle, and reads of a stale result file) rather than to Vim. Hence the split
# above — assert the hazard, not a mechanism.
#
# PASS: with the mapping active, two Escapes are swallowed and ':2d' lands in
# the buffer as literal text.
set -e
unset TMUX
export TMUX_TMPDIR=$(mktemp -d /tmp/vaf-resc-XXXX)
trap 'tmux kill-server 2>/dev/null; rm -rf "$TMUX_TMPDIR"' EXIT
V='set nocompatible noloadplugins | inoremap <Esc> <Nop>'  # worst case: Esc does nothing

rm -f "$TMUX_TMPDIR/b" "$TMUX_TMPDIR/pre"
printf 'LINE-ONE\nLINE-TWO\nLINE-THREE\n' > "$TMUX_TMPDIR/f"
VIMINIT="$V" tmux -f /dev/null new-session -d -s m -x 120 -y 30 "vim $TMUX_TMPDIR/f"
P=$(tmux list-panes -t m -F '#{pane_id}' | head -1)
# Full settle before any key: at ~1s Vim may not have finished sourcing VIMINIT,
# which silently changes the answer and is what made the old conclusion look
# reproducible when it was not.
sleep 2.5

tmux send-keys -t "$P" -l -- ':setlocal modifiable'; tmux send-keys -t "$P" Enter; sleep 0.3
# prove the precondition from inside Vim rather than assuming it
tmux send-keys -t "$P" -l -- ":call writefile(['imap='.maparg('<Esc>','i')],'$TMUX_TMPDIR/pre')"
tmux send-keys -t "$P" Enter; sleep 0.4
if [[ ! -f "$TMUX_TMPDIR/pre" ]] || ! grep -q 'imap=.\+' "$TMUX_TMPDIR/pre"; then
  echo "INCONCLUSIVE: the hostile <Esc> mapping is not active; nothing was tested"
  exit 2
fi
echo "precondition: $(cat "$TMUX_TMPDIR/pre")"

tmux send-keys -t "$P" gg
tmux send-keys -t "$P" o                          # open a line -> insert mode
tmux send-keys -t "$P" -l -- 'typed'; sleep 0.2
tmux send-keys -t "$P" Escape; sleep 0.15
tmux send-keys -t "$P" Escape; sleep 0.4
tmux send-keys -t "$P" -l -- ':2d'; tmux send-keys -t "$P" Enter; sleep 0.4
# force normal for the capture itself, by a route the mapping cannot intercept
tmux send-keys -t "$P" 'C-\' C-n; sleep 0.2
tmux send-keys -t "$P" -l -- ':redir! > '"$TMUX_TMPDIR"'/b | silent %p | redir END'
tmux send-keys -t "$P" Enter; sleep 0.4

echo "== buffer with the hostile mapping active =="
BUF=$(tr -d '\000' < "$TMUX_TMPDIR/b")
print -r -- "$BUF" | grep -v '^[[:space:]]*$'
if print -r -- "$BUF" | grep -q ':2d'; then
  echo "PASS: an active insert-<Esc> mapping swallowed both Escapes, ':2d' landed as text"
  exit 0
fi
echo "INCONCLUSIVE: the Escapes exited insert despite the mapping (vim behavior changed?)"
exit 2
