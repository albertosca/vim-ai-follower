"""Resolve the nvim RPC socket to drive: adopt a running nvim's socket, or
launch a dedicated `nvim --listen` in a tmux split. macOS keeps a socket-less
nvim's socket at $TMPDIR/nvim.$USER/<random>/nvim.<pid>.0 (XDG_RUNTIME_DIR is
unset), so adoption globs by the pane's nvim pid."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from vim_ai_follower import state
from vim_ai_follower.tmux import TmuxPane

# Bounded wait for the launched nvim to bind its RPC socket before we return
# control to the caller. The socket file's mere existence is a sound
# readiness proxy for a unix-domain listening socket. Heavy configs can take
# a while to boot, hence the generous ceiling; if it times out we still
# return (no worse than the old unconditional-return behavior) and the
# subsequent pynvim.attach() raises the same clear OSError as before.
_SOCKET_WAIT_SECONDS = 3.0
_SOCKET_POLL_INTERVAL_SECONDS = 0.02


def launch_socket_path(window_id: str, base_dir: Path | None = None) -> Path:
    return state.nvim_socket_path(window_id, base_dir)


def discover_adopt_socket(pane_id: str) -> str | None:
    pid = TmuxPane(pane_id=pane_id).pane_pid()
    if pid is None:
        return None
    tmpdir = os.environ.get("TMPDIR", "/tmp")
    user = os.environ.get("USER", "")
    matches = sorted(Path(tmpdir, f"nvim.{user}").glob(f"*/nvim.{pid}.0"))
    return str(matches[0]) if matches else None


def resolve_nvim_target(origin_pane: str, window_id: str, adopt: bool) -> tuple[str, bool]:
    if adopt:
        found = discover_adopt_socket(origin_pane)
        if found is not None:
            return found, False
    sock = str(launch_socket_path(window_id))
    subprocess.run(
        ["tmux", "split-window", "-h", "-t", origin_pane, "nvim", "--listen", sock],
        check=True,
    )
    deadline = time.monotonic() + _SOCKET_WAIT_SECONDS
    while not Path(sock).exists() and time.monotonic() < deadline:
        time.sleep(_SOCKET_POLL_INTERVAL_SECONDS)
    return sock, True


def standalone_launch_command(
    sock: str, *, has_nvim_qt: bool, has_vimr: bool, is_iterm: bool
) -> list[str]:
    """Argv to open a VISIBLE nvim listening on `sock`, preferring a native
    GUI, then a split pane inside the user's own iTerm2 window/tab (so it
    lands beside their existing work instead of a disconnected process),
    and only falling back to a fresh Terminal.app window (always present
    on macOS) when neither a GUI app nor iTerm2 is available."""
    if has_nvim_qt:
        return ["nvim-qt", "--", "--listen", sock]
    if has_vimr:
        return ["open", "-a", "VimR", "--args", "--listen", sock]
    if is_iterm:
        # Verified against the installed iTerm2.sdef (2026-09-04): `split
        # vertically with default profile` is a command on the `session`
        # class, taking an optional `command` parameter. Targeting the
        # bundle id (not "iTerm2" by name) is immune to a future app
        # rename. This puts nvim in a NEW pane beside the user's current
        # work, in the SAME window/tab — the thing Alberto asked for
        # instead of a disconnected Terminal.app window (2026-09-03).
        script = (
            'tell application id "com.googlecode.iterm2"\n'
            "  tell current session of current window\n"
            f'    split vertically with default profile command "nvim --listen {sock}"\n'
            "  end tell\n"
            "end tell"
        )
        return ["osascript", "-e", script]
    # `do script` alone can create the window WITHOUT bringing it on screen: if
    # Terminal.app is already running (even with no windows, or backgrounded on
    # another Space), the new window comes back `visible=false` and Terminal
    # never becomes frontmost — the command reports success but nothing is
    # visible to the user (live finding, 2026-09-03). `activate` first is what
    # actually shows the window.
    script = (
        'tell app "Terminal" to activate\n'
        f'tell app "Terminal" to do script "exec nvim --listen {sock}"'
    )
    return ["osascript", "-e", script]


def _vimr_app_present() -> bool:
    return Path("/Applications/VimR.app").exists()


def launch_standalone_nvim(window_id: str) -> str:
    sock = str(launch_socket_path(window_id))
    cmd = standalone_launch_command(
        sock,
        has_nvim_qt=shutil.which("nvim-qt") is not None,
        has_vimr=shutil.which("VimR") is not None or _vimr_app_present(),
        is_iterm=os.environ.get("TERM_PROGRAM") == "iTerm.app",
    )
    subprocess.run(cmd, check=True)
    deadline = time.monotonic() + _SOCKET_WAIT_SECONDS
    while not Path(sock).exists() and time.monotonic() < deadline:
        time.sleep(_SOCKET_POLL_INTERVAL_SECONDS)
    return sock
