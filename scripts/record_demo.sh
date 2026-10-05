#!/usr/bin/env bash
# Record the scripted demo (assets/demo/follow.tape) into a frames directory.
#
#   scripts/record_demo.sh <frames-dir>      then   scripts/render_demo.sh <frames-dir>
#
# Builds a throwaway world under /tmp/vafdemo.XXXX: its own HOME (with the
# gruvbox colorscheme and a .vimrc), WORK, XDG dirs and a PRIVATE tmux server
# on its own socket. The follower, the hooks and the tmux prefix keys are all
# real; only the hook payloads and the "user" keystrokes are scripted.
#
# tmux safety: $TMUX overrides TMUX_TMPDIR, so every tmux call here runs with
# TMUX/TMUX_PANE unset AND names the socket. The EXIT trap kills the demo
# server by socket, then proves by pid that nothing from the world survives.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
FRAMES_OUT=${1:?usage: scripts/record_demo.sh <frames-dir>}
LOGS_OUT="${FRAMES_OUT%/}-logs"
if [[ -e "$FRAMES_OUT" ]]; then
    echo "record_demo: $FRAMES_OUT already exists; pick a fresh directory" >&2
    exit 1
fi

# gruvbox (MIT, per its README and package.json), fetched at record time and
# pinned by commit + checksum: morhetz/gruvbox master as of 2026-09-23.
GRUVBOX_COMMIT=ef8864bb42bf244f0295d1c5a403b27e3d139695
GRUVBOX_URL="https://raw.githubusercontent.com/morhetz/gruvbox/$GRUVBOX_COMMIT/colors/gruvbox.vim"
GRUVBOX_SHA256=55116926ba2b625837d9ae89349a5688d60d0b32acdbd8887e1c0d225f079c3d

for tool in vhs tmux vim curl shasum; do
    command -v "$tool" > /dev/null || { echo "record_demo: $tool not found" >&2; exit 1; }
done
[[ -x "$REPO/.venv/bin/python3" ]] || { echo "record_demo: run 'uv sync --extra dev' first" >&2; exit 1; }

# Short on purpose: a Unix socket path is capped near 104 bytes. Resolved to
# its realpath (/tmp is a symlink to /private/tmp on macOS): with WORK spelled
# /tmp/... while the follower opens files by realpath, Vim could not shorten
# the names against its cwd and showed the scratch path in the tabline and in
# the "written" message.
R=$(mktemp -d /tmp/vafdemo.XXXX)
R=$(cd "$R" && pwd -P)
SOCK="$R/s/demo.sock"

tmux_demo() { env -u TMUX -u TMUX_PANE tmux -S "$SOCK" "$@"; }

# Every live process that names the world in its argv or its environment.
# ps runs to completion before grep starts, so grep's own argv never matches.
world_processes() {
    local table
    table=$(ps -Aww -E -o pid=,command=)
    printf '%s\n' "$table" | grep -F -- "$R" || true
}

cleanup() {
    local status=$? server_pid="" survivors="" i
    if [[ -S "$SOCK" ]]; then
        server_pid=$(tmux_demo display-message -p '#{pid}' 2> /dev/null || true)
        tmux_demo kill-server 2> /dev/null || true
    fi
    for ((i = 0; i < 50; i++)); do
        survivors=$(world_processes)
        if [[ -z "$survivors" ]] && { [[ -z "$server_pid" ]] || ! kill -0 "$server_pid" 2> /dev/null; }; then
            break
        fi
        sleep 0.1
    done
    if [[ -n "$survivors" ]] || { [[ -n "$server_pid" ]] && kill -0 "$server_pid" 2> /dev/null; }; then
        echo "record_demo: survivors after kill-server (killing -9):" >&2
        printf '%s\n' "$survivors" >&2
        [[ -n "$server_pid" ]] && kill -9 "$server_pid" 2> /dev/null || true
        printf '%s\n' "$survivors" | awk '{print $1}' | xargs kill -9 2> /dev/null || true
        status=1
    else
        echo "record_demo: demo server ${server_pid:-(never started)} gone; no process names $R"
    fi
    # Keep the world's logs beside the frames: the driver's stderr and the
    # follower's own hook.log are the only record of what went wrong.
    mkdir -p "$LOGS_OUT"
    cp "$R/w/driver.err" "$R/w/hooks.log" "$R/h/.cache/claude-vim-follower/hook.log" "$LOGS_OUT/" 2> /dev/null || true
    case "$R" in /tmp/vafdemo.* | /private/tmp/vafdemo.*) rm -rf "$R" ;; esac
    exit "$status"
}
trap cleanup EXIT

mkdir -p "$R/h/.vim/colors" "$R/w" "$R/s" "$R/h/.config" "$R/h/.cache" "$R/h/.local/state"
curl -fsSL "$GRUVBOX_URL" -o "$R/h/.vim/colors/gruvbox.vim"
echo "$GRUVBOX_SHA256  $R/h/.vim/colors/gruvbox.vim" | shasum -a 256 -c - > /dev/null
cat > "$R/h/.vimrc" << 'EOF'
syntax on
set background=dark
colorscheme gruvbox
EOF

cat > "$R/tmux.conf" << 'EOF'
set -g default-terminal "screen-256color"
set -g status-style "bg=colour237,fg=colour223"
set -g status-left " vim-ai-follower demo "
set -g status-left-length 40
set -g status-right ""
set -g window-status-format ""
set -g window-status-current-format ""
set -g pane-border-style "fg=colour246"
set -g pane-active-border-style "fg=colour246"
EOF

# What the tape types (hidden) inside the vhs terminal: a clean environment
# holding only the world, and the demo server on its own socket.
cat > "$R/launch.sh" << EOF
exec env -i HOME=$R/h WORK=$R/w TMPDIR=$R/w TMUX_TMPDIR=$R/s \\
    XDG_CONFIG_HOME=$R/h/.config XDG_CACHE_HOME=$R/h/.cache XDG_STATE_HOME=$R/h/.local/state \\
    PATH=$REPO/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin \\
    TERM=xterm-256color LANG=en_US.UTF-8 SHELL=/bin/bash \\
    tmux -S $SOCK -f $R/tmux.conf new-session -s demo "bash $REPO/assets/demo/driver.sh"
EOF

(cd "$R" && env -u TMUX -u TMUX_PANE vhs "$REPO/assets/demo/follow.tape")

frames=$(find "$R/frames" -name 'frame-text-*.png' | wc -l | tr -d ' ')
if [[ "$frames" -eq 0 ]]; then
    echo "record_demo: vhs wrote no frames" >&2
    exit 1
fi
mv "$R/frames" "$FRAMES_OUT"
echo "record_demo: $frames frames in $FRAMES_OUT"
