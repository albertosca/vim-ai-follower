#!/bin/zsh
# Autonomous, hermetic end-to-end smoke driven entirely through the real
# claude-follow CLI + hooks. Private tmux socket + plugin-free Vim, so it never
# touches your tmux and never prompts the keychain. Exercises: start opens a
# pane, the color cue tints on the 2nd writer, stop restores the border,
# and two windows stay isolated. (The Escape+undo fix under REAL CoC popups
# still needs a manual run — its hermetic proof is scripts/repro-stray-u.sh.)
#
# Everything targets tmux by PANE ID, never window index, so a base-index in
# ~/.tmux.conf can't throw it off.
set -u
CF=/Users/albertosca/Programming/vim-ai-follower/.venv/bin/claude-follow
FIX="$(cd "$(dirname "$0")/../qa/fixtures" && pwd)"
unset TMUX
export TMUX_TMPDIR=$(mktemp -d /tmp/vaf-auto-XXXX)
export VIMINIT='set nocompatible noloadplugins'
trap 'tmux kill-server 2>/dev/null; rm -rf "$TMUX_TMPDIR" /tmp/vaf-auto-*.py' EXIT

pass=0; fail=0
ok()  { print -r -- "PASS: $1"; pass=$((pass+1)); }
bad() { print -r -- "FAIL: $1"; fail=$((fail+1)); }
panes_in()  { tmux list-panes -t "$1" -F '#{pane_id}' 2>/dev/null; }        # $1 = any pane in the window
count_in()  { panes_in "$1" | wc -l | tr -d ' '; }
other_pane(){ panes_in "$1" | grep -v "^$1\$" | head -1; }                  # the non-origin pane
bstyle()    { tmux show-options -pv -t "$1" pane-border-style 2>/dev/null | tr -d '[:space:]'; }
bstatus()   { tmux show-options -wv -t "$1" pane-border-status 2>/dev/null | tr -d '[:space:]'; }
title()     { tmux display-message -p -t "$1" '#{pane_title}' 2>/dev/null; }
edit() {  # $1 origin-pane, $2 file, $3 fixture, $4 id-json
  cp "$FIX/$3" "$2"
  local p="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$2\"}$4}"
  echo "$p" | TMUX_PANE=$1 "$CF" hook pre >/dev/null 2>&1
  echo "$p" | TMUX_PANE=$1 "$CF" hook post >/dev/null 2>&1
}

tmux new-session -d -s auto -x 200 -y 50
O0=$(tmux list-panes -t auto -F '#{pane_id}' | head -1)

# --- Prep: start opens a follower pane ---
TMUX_PANE=$O0 "$CF" start >/dev/null 2>&1
sleep 1
[[ "$(count_in $O0)" == 2 ]] && ok "start opened a follower pane" || bad "start did NOT open a pane (panes=$(count_in $O0))"
F0=$(other_pane "$O0")

if [[ -n "$F0" ]]; then
  # --- Check 1: color cue (neutral -> tinted+label on 2nd writer) ---
  edit "$O0" /tmp/vaf-auto.py cue-writer1.py ',"session_id":"you"'
  [[ -z "$(bstyle $F0)" ]] && ok "1 writer -> border neutral" || bad "1 writer border not neutral: [$(bstyle $F0)]"
  edit "$O0" /tmp/vaf-auto.py cue-writer2.py ',"session_id":"you","agent_id":"rev1","agent_type":"code-reviewer"'
  [[ "$(bstyle $F0)" == fg=* ]] && ok "2nd writer -> border tinted [$(bstyle $F0)]" || bad "2nd writer border NOT tinted: [$(bstyle $F0)]"
  [[ "$(title $F0)" == "code-reviewer" ]] && ok "2nd writer -> title 'code-reviewer'" || bad "border title wrong: [$(title $F0)]"

  # --- Check 2: stop restores the border ---
  TMUX_PANE=$O0 "$CF" stop >/dev/null 2>&1
  sleep 1
  [[ -z "$(bstatus $O0)" ]] && ok "stop cleared pane-border-status" || bad "stop left pane-border-status=[$(bstatus $O0)]"
  [[ "$(count_in $O0)" == 1 ]] && ok "stop closed the follower pane" || bad "stop left panes=$(count_in $O0)"
else
  bad "no follower pane to test the cue against"
fi

# --- Check 4: window-scoping isolation ---
O1=$(tmux new-window -t auto -P -F '#{pane_id}')
TMUX_PANE=$O0 "$CF" start >/dev/null 2>&1; sleep 1
TMUX_PANE=$O1 "$CF" start >/dev/null 2>&1; sleep 1
F0b=$(other_pane "$O0"); F1b=$(other_pane "$O1")
edit "$O0" /tmp/vaf-auto-a.py window-alpha.py ',"session_id":"w"'
edit "$O1" /tmp/vaf-auto-b.py window-beta.py ',"session_id":"w"'
if [[ -n "$F0b" && -n "$F1b" && "$F0b" != "$F1b" ]]; then
  ok "two windows -> two distinct follower panes ($F0b vs $F1b)"
  c0=$(tmux capture-pane -t "$F0b" -p); c1=$(tmux capture-pane -t "$F1b" -p)
  { echo "$c0" | grep -q "WINDOW ZERO" && ! echo "$c0" | grep -q "WINDOW ONE"; } && ok "window 0 shows only its own edit" || bad "window 0 bleed/empty"
  { echo "$c1" | grep -q "WINDOW ONE"  && ! echo "$c1" | grep -q "WINDOW ZERO"; } && ok "window 1 shows only its own edit" || bad "window 1 bleed/empty"
else
  bad "window-scoping: expected two distinct follower panes (got '$F0b' / '$F1b')"
fi

echo "----"
echo "RESULT: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
