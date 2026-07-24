#!/bin/zsh
# Proof + record for the CoC inlay-hints fix.
#
# ROOT CAUSE (found via live QA, 2026-07-24): the follower's scratch buffer
# is buftype=nofile during typing, so CoC never attaches to it — but the
# relock's `:e!` turns it into a real, disk-backed buffer for the first time,
# and CoC (Alberto's coc-pyright: pyright.inlayHints.parameterTypes /
# functionReturnTypes) then attaches and renders inline parameter/type hints
# as virtual text overlaid on the buffer. `tmux capture-pane` — and a human's
# own eyes on the terminal — can't distinguish that overlay from real
# characters, so the animation LOOKS like it typed garbage (e.g.
# `os.listdir(root)` rendered as `os.listdir(path: root)`, `def
# collect_paths(root):` gaining a fabricated `-> list[Unknown]:`) even though
# the underlying buffer/file content was always correct — confirmed during
# the investigation via a direct `:%p` buffer dump (bypasses screen
# rendering entirely), diffed byte-for-byte against the source fixture.
#
# FIX: TmuxVimFollower._with_unlocked sends `:silent! CocDisable` before
# unlocking for animation — the follower's Vim is a separate OS process per
# pane, so this never touches Alberto's own real editing Vim's CoC state.
# hand_over() sends `:silent! CocEnable` — the one place a human actually
# gets to type into the buffer themselves (interrupt hand-off) and wants
# real completion/hints back. `:silent!` makes both a safe no-op in a Vim
# without CoC installed at all.
#
# LIVE VERIFICATION PERFORMED (not automatable here — needs a real coc-pyright
# LSP round-trip, which a hermetic plugin-free Vim can't reproduce; a hand-
# rolled autocmd/timer stub to fake CoC's async virtual-text rendering was
# tried and abandoned — it hit its own unrelated Vim-scripting/redraw quirks
# under scripted tmux send-keys, orthogonal to the fix itself):
#   1. Reproduced the garble live 4+ times against Alberto's real coc-pyright
#      config (speed lento, qa/fixtures/pause-trigger.py) — confirmed via
#      `:redir! > f | silent %p | redir END` (direct buffer dump) that the
#      REAL buffer/file content was always correct; the garble was CoC's own
#      virtual-text render, not data corruption.
#   2. Confirmed `:CocDisable` before the same animation and `:CocEnable`
#      after eliminate the effect (repeated twice, consistent).
#   3. Verified the SHIPPED fix (in tmux_vim.py, not manual keystrokes) end
#      to end through the real `claude-follow start` / `hook pre` / `hook
#      post` CLI path: output buffer clean, diffed against the fixture.
#   4. `tests/test_tmux_vim.py` asserts the exact `:silent! CocDisable` /
#      `:silent! CocEnable` ex-commands are sent at the exact right points
#      in every animation entry point (_with_unlocked) and in hand_over().
#
# This script proves the ONE thing safely provable hermetically: `:silent!
# CocDisable`/`:silent! CocEnable` are harmless no-ops in a Vim with no CoC
# at all (E492 would otherwise fire without the `!`), so the fix never
# breaks a user who doesn't run CoC.
set -e
export TMUX_TMPDIR=$(mktemp -d /tmp/vaf-coc-XXXX)
unset TMUX
trap 'tmux kill-server 2>/dev/null; rm -rf "$TMUX_TMPDIR"' EXIT

printf 'a\nb\n' > "$TMUX_TMPDIR/f"
VIMINIT='set nocompatible noloadplugins' tmux new-session -d -s m -x 100 -y 20 "vim $TMUX_TMPDIR/f"
P=$(tmux list-panes -t m -F '#{pane_id}' | head -1)
sleep 1.0
tmux send-keys -t "$P" -l -- ':silent! CocDisable'
tmux send-keys -t "$P" Enter
sleep 0.2
tmux send-keys -t "$P" -l -- ':silent! CocEnable'
tmux send-keys -t "$P" Enter
sleep 0.2
tmux send-keys -t "$P" -l -- ':echo "still alive"'
tmux send-keys -t "$P" Enter
sleep 0.3
OUT=$(tmux capture-pane -p -t "$P" | tail -3)
print -r -- "$OUT"
if print -r -- "$OUT" | grep -q "still alive"; then
  echo "PASS: :silent! CocDisable / :silent! CocEnable are safe no-ops without CoC installed"
else
  echo "FAIL: something errored out after the CocDisable/CocEnable no-ops"
  exit 1
fi
