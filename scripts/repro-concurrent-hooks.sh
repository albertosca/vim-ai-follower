#!/bin/zsh
# Regression: parallel PostToolUse hooks must NOT animate concurrently into the
# same follower pane. Six Write tool calls fire six hooks near-simultaneously;
# before the fix they all passed a check-then-act guard on the .animating marker
# and interleaved their unlock/wipe/opener/insert keystrokes into garble like
#   bravo_1:%d:%dfoxtrot_1delta_1alpha_1   (four files' content on one line)
#   ii^\o^\obravo_2                        (openers/C-\ leaking as literal text)
# — the exact shape a real six-language run produced (2026-07-23).
#
# control.try_acquire_animating makes the guard an atomic O_EXCL claim: exactly
# one hook wins and animates, the other five skip. This script drives six real
# `hook post` OS processes at once and asserts a clean result: no line carries
# two files' tokens, nothing opens with a literal o/O, and the follower vim
# holds exactly ONE tab. Fully isolated (HOME override -> temp tmux-backend
# config + hermetic vim; private tmux socket); never touches the real config.
#
# PASS: single tab, zero interleave, zero literal openers.
set -e
REPO="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
export HOME=$(mktemp -d /tmp/vaf-conc-home-XXXX)
export TMUX_TMPDIR=$(mktemp -d /tmp/vaf-conc-tmux-XXXX)
unset TMUX
CLI() { PYTHONPATH="$REPO/src" python3 -m vim_ai_follower.cli "$@"; }
cleanup() { tmux kill-server 2>/dev/null; rm -rf "$HOME" "$TMUX_TMPDIR"; }
trap cleanup EXIT

mkdir -p "$HOME/.config/claude-vim-follower"
printf '{"open_policy": "manual", "backend": "tmux"}\n' > "$HOME/.config/claude-vim-follower/config.json"

TAGS=(alpha bravo charlie delta echo foxtrot)
for t in $TAGS; do : > "$HOME/$t.txt"; for i in $(seq -w 1 8); do print -r -- "${t}_${i}" >> "$HOME/$t.txt"; done; done

tmux new-session -d -s w -x 180 -y 45
ORIGIN=$(tmux list-panes -t w -F '#{pane_id}' | head -1)
sleep 0.5
TMUX_PANE="$ORIGIN" CLI start >/dev/null 2>&1 || true
for _ in $(seq 1 20); do [[ $(tmux list-panes -t w | wc -l) -ge 2 ]] && break; sleep 0.2; done
FOLLOWER=$(tmux list-panes -t w -F '#{pane_id}' | grep -v "$ORIGIN" | head -1)
sleep 1.0

post() { print -r -- "{\"tool_name\": \"Write\", \"tool_input\": {\"file_path\": \"$1\"}}" | TMUX_PANE="$ORIGIN" CLI hook post >/dev/null 2>&1; }
echo "=== firing 6 concurrent hook-post processes ==="
for t in $TAGS; do post "$HOME/$t.txt" & done

SNAP="$HOME/snaps.txt"; : > "$SNAP"
for n in $(seq 1 14); do tmux capture-pane -p -t "$FOLLOWER" 2>/dev/null >> "$SNAP"; sleep 0.3; done
wait
sleep 1.0

tmux send-keys -t "$FOLLOWER" 'C-\' 'C-n' 2>/dev/null; sleep 0.2
tmux send-keys -t "$FOLLOWER" -l -- ':redir! > '"$HOME"'/tabs.txt | silent tabs | redir END'; tmux send-keys -t "$FOLLOWER" Enter; sleep 0.3
TABS=$(tr -d '\000' < "$HOME/tabs.txt" 2>/dev/null | grep -c 'Tab page' || true)

FILE_TAGS='alpha|bravo|charlie|delta|echo|foxtrot'
interleave=$(grep -cE "(${FILE_TAGS})_[0-9].*(${FILE_TAGS})_[0-9]" "$SNAP" || true)
literal_open=$(grep -cE '^[[:space:]]*[oO][a-z]' "$SNAP" || true)
distinct_tabs_content=$(grep -oE "(${FILE_TAGS})_[0-9]" "$SNAP" | sed -E 's/_[0-9]//' | sort -u | tr '\n' ' ')

echo "=== result ==="
echo "follower tabs: $TABS   (want exactly 1)"
echo "snapshot lines with TWO files' tokens: $interleave   (want 0)"
echo "snapshot lines starting with a literal o/O opener: $literal_open   (want 0)"
echo "file tags that appeared in the pane: $distinct_tabs_content"
if [[ "$TABS" -eq 1 && "$interleave" -eq 0 && "$literal_open" -eq 0 ]]; then
  echo "PASS: exactly one hook animated; no concurrent-animation garble"
else
  echo "FAIL: concurrent-animation garble present (guard is not atomic)"
  echo "--- a busy snapshot ---"; grep -vE '^[[:space:]]*~?[[:space:]]*$' "$SNAP" | head -12
  exit 1
fi
