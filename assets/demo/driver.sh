#!/usr/bin/env bash
# The scripted demo, run INSIDE the demo's own throwaway tmux server (the left
# pane) by scripts/record_demo.sh. Everything on screen is the real follower:
# `claude-follow start`, real `hook pre`/`hook post` calls fed the JSON payload
# Claude Code would send (the same shape tests/e2e_harness.py uses), and the
# real tmux prefix keys, pressed through the attached client with
# `send-keys -K` so they go through tmux's prefix table exactly like a person's
# keystrokes. Only the payloads and the "user" typing are scripted; no Claude
# API call is made.
#
# Never run this by hand from your own tmux: it expects the isolated world
# (HOME, WORK, TMUX) that record_demo.sh builds.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)
CF="$REPO/bin/claude-follow"
: "${WORK:?WORK is set by scripts/record_demo.sh}"
cd "$WORK"
exec 2>> "$WORK/driver.err"
set -x
FILE="$WORK/fib.py"
HOOK_LOG="$WORK/hooks.log"

YELLOW=$'\e[1;33m'
AQUA=$'\e[1;36m'
DIM=$'\e[2m'
RESET=$'\e[0m'

claude_says() { printf '%sClaude:%s %s\n' "$YELLOW" "$RESET" "$1"; }
you_do() { printf '%syou:%s %s\n' "$AQUA" "$RESET" "$1"; }
note() { printf '%s%s%s\n' "$DIM" "$1" "$RESET"; }

payload() {
    printf '{"tool_name":"%s","tool_input":{"file_path":"%s"},"session_id":"demo"}' "$1" "$2"
}

window_id=$(tmux display-message -p '#{window_id}')
ANIMATING="$HOME/.cache/claude-vim-follower/$window_id.animating"

# The live animation's state, read the way control.animating_state reads it:
# "<pid> [state]", and only while that pid is alive.
animating_state() {
    local marker pid state
    # cat rather than `read < file`: the marker has no trailing newline, and
    # `read` reports failure at EOF even after it has filled the variables.
    marker=$(cat "$ANIMATING" 2> /dev/null) || return 0
    read -r pid state <<< "$marker"
    kill -0 "$pid" 2>/dev/null || return 0
    printf '%s\n' "${state:-running}"
}

wait_for_state() {
    local want=$1 i
    for ((i = 0; i < 300; i++)); do
        [[ $(animating_state) == "$want" ]] && return 0
        sleep 0.1
    done
    note "(timed out waiting for the animation to be $want)"
    return 1
}

# Timing cue only (never a verdict): the follower pane's rendered screen shows
# `needle`. The edits animate in a few seconds at --speed normal, so the
# controls are keyed to what is on screen rather than to a sleep.
wait_for_screen() {
    local needle=$1 i
    for ((i = 0; i < 300; i++)); do
        tmux capture-pane -p -t "$follower" | grep -qF -- "$needle" && return 0
        sleep 0.05
    done
    note "(timed out waiting for $needle on screen)"
    return 1
}

# Press a tmux prefix key for real: -K routes the keys through the attached
# client's key tables, so C-b P runs the binding `claude-follow start`
# registered (run-shell `claude-follow pause`), not a direct CLI call.
press_prefix() {
    local client
    client=$(tmux list-clients -F '#{client_name}' | head -n 1)
    tmux send-keys -K -c "$client" C-b "$1"
}

# The "user" typing into the follower pane, one key at a time.
type_into() {
    local pane=$1 text=$2 i
    for ((i = 0; i < ${#text}; i++)); do
        tmux send-keys -t "$pane" -l "${text:i:1}"
        sleep 0.06
    done
}

clear
tmux select-pane -T "Claude Code session"
note "\$ claude-follow start --speed normal"
"$CF" start --backend tmux --speed normal
follower=$(tmux list-panes -F '#{pane_id}' | grep -v "^$TMUX_PANE\$" | head -n 1)
tmux select-pane -t "$TMUX_PANE"
sleep 2

# --- 1. A Write, paused mid-way -------------------------------------------
echo
claude_says "Write fib.py"
payload Write "$FILE" | "$CF" hook pre
cat > "$FILE" <<'PY'
"""Fibonacci numbers."""


def fib(n):
    """Return the n-th Fibonacci number."""
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


if __name__ == "__main__":
    print([fib(i) for i in range(10)])
PY
payload Write "$FILE" | "$CF" hook post >> "$HOOK_LOG" 2>&1 &
first=$!
wait_for_state running
wait_for_screen "for _ in range"

you_do "[prefix P] pause"
press_prefix P
wait_for_state paused
sleep 3
you_do "[prefix P] resume"
press_prefix P
wait "$first"
sleep 1.5

# --- 2. A second file, interrupted mid-typing and taken over ---------------
# Both steps are Writes of new files on purpose. An Edit of an existing file
# was tried and dropped: on the tmux backend, the navigation before the diff
# reloaded the already-written file from disk, so the finished content
# flashed up first, the ops then typed duplicates on top, and the closing
# relock snapped it back (measured 2026-09-23, with and without this demo's
# .vimrc). That is a product bug to fix, not something to showcase.
TESTS="$WORK/test_fib.py"
echo
claude_says "Write test_fib.py"
payload Write "$TESTS" | "$CF" hook pre
cat > "$TESTS" <<'PY'
from fib import fib


def test_first_seven():
    assert [fib(i) for i in range(7)] == [0, 1, 1, 2, 3, 5, 8]


def test_zero_and_one():
    assert fib(0) == 0
    assert fib(1) == 1


def test_never_shrinks():
    assert all(fib(i) <= fib(i + 1) for i in range(30))
PY
payload Write "$TESTS" | "$CF" hook post > "$WORK/tests-hook.out" 2>> "$HOOK_LOG" &
second=$!
wait_for_state running
wait_for_screen "def test_zero"

you_do "[prefix S] interrupt: take over"
press_prefix S
wait_for_state handoff
sleep 2.5

you_do "type a line of your own, then :w!"
tmux send-keys -t "$follower" G o
type_into "$follower" "# you: negative n still needs a test"
tmux send-keys -t "$follower" Escape
sleep 1
# :w! rather than :w: a fresh file's buffer was renamed in place, so a plain
# :w stops at E13 "File exists" (measured 2026-09-23), even though the
# border cue says ":w releases". The hand-off message in cmd_pause says :w!.
type_into "$follower" ":w!"
tmux send-keys -t "$follower" Enter
wait "$second"
sleep 0.5

echo
note "The hook releases Claude's turn and tells it:"
# A verbatim excerpt of the context the hook hands Claude: its opening
# sentences and its closing instruction (the quoted partial is elided).
python3 - "$WORK/tests-hook.out" <<'PYEOF' | fold -s -w "$(($(tmux display-message -p '#{pane_width}') - 4))" | sed 's/^/  /'
import json
import os
import sys

with open(sys.argv[1]) as handle:
    context = json.load(handle)["hookSpecificOutput"]["additionalContext"]
# Longest first: /tmp/... is a suffix of its /private/tmp/... realpath.
for path in sorted({os.path.realpath(os.environ["WORK"]), os.environ["WORK"]}, key=len, reverse=True):
    context = context.replace(os.path.join(path, "test_fib.py"), "test_fib.py")
paragraphs = context.split("\n\n")
opening = paragraphs[0].split(" Only this much")[0]
print(f"{opening} [...]\n{paragraphs[-1]}")
PYEOF
sleep 7
echo
note "end of demo"
sleep 120
