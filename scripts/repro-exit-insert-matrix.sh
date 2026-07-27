#!/bin/zsh
# Regression guard for the insert-exit sequence (animate._EXIT_INSERT).
#
# THE BUG THIS GUARDS AGAINST (found 2026-07-27): _EXIT_INSERT used to end with
# <C-\><C-n> as insurance against a remapped insert-mode <Esc>. That pair
# corrupted the buffer on EVERY animation, in two independent ways:
#   1. send_paced sleeps pace_seconds between every KeySequence, splitting what
#      is ONE atomic Vim command. With a gap, Vim expires the pending <C-\> and
#      runs <C-N> as plain insert-mode keyword completion, replacing the word
#      under the cursor with the first keyword in the buffer.
#   2. Sent after the Escapes it arrives in NORMAL mode, where <C-\> can be a
#      real user mapping (vim-tmux-navigator binds it to :TmuxNavigatePrevious)
#      and the trailing <C-N> then moves the cursor down, drifting the insert
#      point.
#
# WHY THIS WENT UNNOTICED FOR SO LONG: the relock runs `:silent! e!`, which
# reloads the file from disk — and Claude has already written the correct file
# by the time the hook animates. So the final buffer always looked right. This
# script therefore dumps the buffer BEFORE the relock. Never judge an animation
# bug from a post-relock dump, and never from `tmux capture-pane` (a rendered
# SCREEN is not the buffer; that trap has fooled this project repeatedly).
#
# Vim reports its own mode here via a ModeChanged autocmd rather than the screen
# being read: a healthy run leaves Insert exactly once per source line.
#
# Runs against the REAL user config on purpose (like repro-plugin-preamble.sh
# and repro-reedit-realconfig.sh): a plugin-free Vim does NOT reproduce this —
# it needs a Normal-mode <C-\> mapping to show failure mode 2.
#
# PASS: the shipped two-Escape exit leaves the buffer byte-identical to the
# source AND leaves Insert once per line, while the old four-key variant does
# not.
set -e
unset TMUX
export TMUX_TMPDIR=$(mktemp -d /tmp/vaf-exitmatrix-XXXX)
trap 'tmux kill-server 2>/dev/null; rm -rf "$TMUX_TMPDIR"' EXIT

PACE=0.15   # the 'lento' pace; the failure is pace-dependent by construction
F="$TMUX_TMPDIR/sample.py"
cat > "$F" <<'PYEOF'
import os


def collect_paths(root):
    results = []
    for name in os.listdir(root):
        full = os.path.join(root, name)
        cleaned = full.strip().lower()
        results.append(cleaned)
    return sorted(set(results))
PYEOF
NLINES=$(wc -l < "$F" | tr -d ' ')

# $1 = shipped | old-four-key ; echoes "<i_to_n_count> <CLEAN|CORRUPTED>"
run_variant() {
  local variant="$1"
  local log="$TMUX_TMPDIR/modes-$variant.log"
  local dump="$TMUX_TMPDIR/buf-$variant"
  : > "$log"; rm -f "$dump"
  tmux kill-session -t x 2>/dev/null || true
  tmux -f /dev/null new-session -d -s x -x 101 -y 51 "vim"
  local P=$(tmux list-panes -t x -F '#{pane_id}' | head -1)
  sleep 3.0                                    # let the real config finish loading
  local T K
  T() { tmux send-keys -t "$P" -l -- "$1"; }
  K() { tmux send-keys -t "$P" "$1"; }

  K Escape; K Escape; sleep 0.3
  T ":autocmd ModeChanged * call writefile([v:event.old_mode.'->'.v:event.new_mode], '$log', 'a')"
  K Enter; sleep 0.3

  # the show_fresh preamble, as backends/tmux_vim.py emits it
  T ":silent! bwipeout! $F"; K Enter
  T ":file $F"; K Enter
  T ":filetype detect"; K Enter
  T ":setlocal buftype="; K Enter
  T ":silent! CocDisable"; K Enter
  T ":setlocal modifiable paste"; K Enter
  T ":%d"; K Enter
  sleep 0.5

  local n=0 opener
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ $n -eq 0 ]]; then opener=i; else opener=o; fi
    T "$opener"; sleep $PACE
    T "$line";   sleep $PACE
    K Escape;    sleep $PACE
    K Escape;    sleep $PACE
    if [[ "$variant" == "old-four-key" ]]; then
      K 'C-\'; sleep $PACE
      K C-n;   sleep $PACE
    fi
    n=$((n+1))
  done < "$F"
  sleep 0.5

  # ground truth BEFORE the relock's :e! would reload the correct file from disk
  K Escape; K Escape; sleep 0.3
  T ":redir! > $dump | silent %p | redir END"; K Enter; sleep 0.6

  local left=$(grep -c 'i->n' "$log" 2>/dev/null || echo 0)
  local verdict=CORRUPTED
  if [[ -f "$dump" ]] && diff -q \
      <(tr -d '\000' < "$dump" | grep -v '^[[:space:]]*$') \
      <(grep -v '^[[:space:]]*$' "$F") >/dev/null 2>&1; then
    verdict=CLEAN
  fi
  echo "$left $verdict"
  tmux kill-session -t x 2>/dev/null || true
}

echo "== shipped exit (two Escapes) =="
shipped=$(run_variant shipped)
echo "   left Insert: ${shipped%% *} of $NLINES   verdict: ${shipped##* }"
tr -d '\000' < "$TMUX_TMPDIR/buf-shipped" | grep -v '^[[:space:]]*$' | sed 's/^/     /'

echo
echo "== old four-key exit (two Escapes + <C-\\><C-n>) =="
old=$(run_variant old-four-key)
echo "   left Insert: ${old%% *} of $NLINES   verdict: ${old##* }"
tr -d '\000' < "$TMUX_TMPDIR/buf-old-four-key" | grep -v '^[[:space:]]*$' | sed 's/^/     /'

echo
if [[ "${shipped##* }" == "CLEAN" && "${shipped%% *}" == "$NLINES" && "${old##* }" == "CORRUPTED" ]]; then
  echo "PASS: the two-Escape exit is clean; re-adding <C-\\><C-n> corrupts the buffer"
  exit 0
fi
if [[ "${shipped##* }" == "CLEAN" && "${shipped%% *}" == "$NLINES" ]]; then
  echo "INCONCLUSIVE: the shipped exit is clean, but the old variant did not fail here."
  echo "  Failure mode 2 needs a Normal-mode <C-\\> mapping (e.g. vim-tmux-navigator)."
  echo "  Check with:  :verbose map <C-Bslash>"
  exit 2
fi
echo "FAIL: the shipped two-Escape exit did not produce a clean buffer"
exit 1
