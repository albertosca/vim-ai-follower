#!/bin/zsh
# Regression check for the plugin-Vim preamble corruption (vim-visual-multi
# + CoC + hit-enter prompts). Runs the follower against the user's REAL Vim
# config — hermetic test Vims cannot reproduce this failure mode — driving
# the exact hook sequence that corrupted buffers live (2026-07-15): three
# files, then a re-edit of the first (the goto_file/tab-drop path).
# PASS: four clean tabs, re-edited content present, no "<Plug>" junk buffer.
set -e
CF=${CLAUDE_FOLLOW:-$(dirname "$0")/../.venv/bin/claude-follow}
PLAY=$(mktemp -d /tmp/vaf-repro-XXXX)
trap 'tmux kill-session -t vaf-repro 2>/dev/null; rm -rf "$PLAY" /tmp/vaf-repro-tabs.txt' EXIT
printf 'def greet(name):\n    return f"Hello, {name}!"\n' > "$PLAY/hello.py"
tmux kill-session -t vaf-repro 2>/dev/null || true
tmux new-session -d -s vaf-repro -x 180 -y 45
ORIGIN=$(tmux list-panes -t vaf-repro -F '#{pane_id}' | head -1)
export TMUX_PANE=$ORIGIN
unset TMUX
"$CF" start
sleep 6  # full-config vim boot (CoC, copilot, VM)
hook() { echo "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$1\"}}" | "$CF" hook "$2"; }
hook "$PLAY/hello.py" pre
printf 'def greet(name):\n    return f"Hello, {name}!"\n\n\ndef shout(name):\n    return greet(name).upper()\n' > "$PLAY/hello.py"
hook "$PLAY/hello.py" post
hook "$PLAY/config.toml" pre
printf '[server]\nhost = "localhost"\nport = 8080\n' > "$PLAY/config.toml"
hook "$PLAY/config.toml" post
hook "$PLAY/util.js" pre
printf 'function add(a, b) {\n  return a + b;\n}\n' > "$PLAY/util.js"
hook "$PLAY/util.js" post
hook "$PLAY/hello.py" pre
printf 'def greet(name):\n    return f"Hi, {name}!"\n\n\ndef shout(name):\n    return greet(name).upper()\n' > "$PLAY/hello.py"
hook "$PLAY/hello.py" post
FOLLOWER=$(tmux list-panes -t vaf-repro -F '#{pane_id}' | tail -1)
sleep 1
tmux send-keys -t "$FOLLOWER" ':redir! > /tmp/vaf-repro-tabs.txt | silent tabs | redir END' Enter
sleep 1
CAPTURE=$(tmux capture-pane -t "$FOLLOWER" -p)
TABS=$(cat /tmp/vaf-repro-tabs.txt)
"$CF" stop
echo "$TABS"
if echo "$TABS" | grep -q "<Plug>"; then echo "FAIL: <Plug> junk buffer"; exit 1; fi
if ! echo "$CAPTURE" | grep -q 'Hi, {name}'; then echo "FAIL: re-edited content missing"; exit 1; fi
echo "PASS"
