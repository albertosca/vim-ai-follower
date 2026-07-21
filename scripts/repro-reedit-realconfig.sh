#!/bin/zsh
# Reproduce the re-edit "esquisitice" against your REAL Vim config (plugins:
# CoC / Copilot / auto-pairs / vim-visual-multi). The follower types under
# `:set paste`, which suppresses autoindent/indentexpr — a hermetic Vim
# re-edits cleanly (verified). So if a garble shows here, a plugin is acting
# even under paste, and THIS run captures it.
#
# Runs in a DEDICATED session on your real server — NEVER kills the server,
# only the `vaf-reedit` session. May prompt the keychain once ("Always Allow").
#
# It edits /tmp/vaf-reedit.py three times (fresh 4 lines -> delete 2 middle
# lines -> add lines back) and dumps the follower buffer after each. Paste me
# the whole output: a correct run has each FOLLOWER block == its DISK block.
set -u
CF="$(cd "$(dirname "$0")/.." && pwd)/bin/claude-follow"
FILE=/tmp/vaf-reedit.py
unset TMUX
tmux has-session -t vaf-reedit 2>/dev/null && { echo "kill it first: tmux kill-session -t vaf-reedit"; exit 1; }
tmux new-session -d -s vaf-reedit -x 160 -y 45
O=$(tmux list-panes -t vaf-reedit -F '#{pane_id}' | head -1)
TMUX_PANE=$O "$CF" start >/dev/null 2>&1
echo "booting your real-config Vim (CoC/Copilot ~6s; keychain prompt possible)..."
sleep 7
F=$(tmux list-panes -t vaf-reedit -F '#{pane_id}' | grep -v "^$O\$" | head -1)
[[ -z "$F" ]] && { echo "FAIL: no follower pane opened"; tmux kill-session -t vaf-reedit; exit 1; }

step() {  # $1 label, $2 content
  printf '%s' "$2" > "$FILE"
  local p="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$FILE\"},\"session_id\":\"me\"}"
  echo "$p" | TMUX_PANE=$O "$CF" hook pre >/dev/null 2>&1
  echo "$p" | TMUX_PANE=$O "$CF" hook post >/dev/null 2>&1
  sleep 1
  echo "======== after $1 ========"
  echo "-- DISK --";     cat "$FILE"
  echo "-- FOLLOWER --"; tmux capture-pane -t "$F" -p | sed '/^~$/d' | sed -e :a -e '/^$/{$d;N;ba}' | tail -20
  echo
}

step "1 fresh (4 lines)" 'def f():
    a = 1
    b = 2
    return a + b
'
step "2 delete 2 middle lines" 'def f():
    return 0
'
step "3 add lines back" 'def f():
    x = 10
    y = 20
    return x + y
'
echo "Done. Cleanup: tmux kill-session -t vaf-reedit"
