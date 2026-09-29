"""Resolve the nvim RPC socket to drive: adopt a running nvim's socket, or
launch a dedicated `nvim --listen` in a tmux split. macOS keeps a socket-less
nvim's socket at $TMPDIR/nvim.$USER/<random>/nvim.<pid>.0 (XDG_RUNTIME_DIR is
unset), so adoption globs by the pane's nvim pid."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from vim_ai_follower import cache, state
from vim_ai_follower.tmux import TmuxPane

# Bounded wait for the launched nvim to bind its RPC socket before we return
# control to the caller. The socket file's mere existence is a sound
# readiness proxy for a unix-domain listening socket. Heavy configs can take
# a while to boot, hence the generous ceiling; if the tmux split times out we
# still return (no worse than the old unconditional-return behavior) and the
# subsequent pynvim.attach() raises the same clear OSError as before.
_SOCKET_WAIT_SECONDS = 3.0
# A standalone window is a GUI app's or a terminal app's cold launch, slower
# than a tmux split, and here the wait is also the verdict: past it the launch
# is reported as failed (NvimNeverListened) instead of attached. The number is
# a generous ceiling, not a measurement.
_STANDALONE_SOCKET_WAIT_SECONDS = 10.0
# The hook auto-open path cannot afford that: a hook blocks the tool call it
# runs in, so every Edit/Write/Read would stall the full wait on a launcher
# that starts nothing. It waits briefly, and after a failure the window backs
# off auto-opening for LAUNCH_BACKOFF_SECONDS (`claude-follow start` is the
# explicit, patient retry and clears the backoff).
AUTO_OPEN_SOCKET_WAIT_SECONDS = 3.0
LAUNCH_BACKOFF_SECONDS = 300.0
_SOCKET_POLL_INTERVAL_SECONDS = 0.02


class StandaloneLaunchFailed(Exception):
    """A standalone nvim could not be brought up: nothing to attach to."""


class SocketPathBlocked(StandaloneLaunchFailed):
    """Something nobody answers on sits at the socket path and cannot be
    removed (a directory, a file we may not delete): nvim could never bind."""

    def __init__(self, sock: str, cause: OSError) -> None:
        super().__init__(f"cannot clear {sock}: {cause}")
        self.sock = sock


class NvimNeverListened(StandaloneLaunchFailed):
    """The standalone launcher returned, but no nvim listened on the socket
    before the deadline: nothing to attach to."""

    def __init__(self, sock: str, launcher: str, waited: float) -> None:
        super().__init__(f"{launcher} exited but no nvim listened on {sock} within {waited:g} s")
        self.sock = sock
        self.launcher = launcher


def launch_socket_path(window_id: str, base_dir: Path | None = None) -> Path:
    return state.nvim_socket_path(window_id, base_dir)


def _socket_path_in_existing_dir(window_id: str) -> Path:
    """The launch socket path, with its directory created: nvim refuses
    `--listen` into a missing directory ("Failed to --listen: no such file
    or directory") and exits, so the follower never comes up. On a fresh
    install nothing may have created the cache dir yet — outside tmux no
    keybinding claim writes there before `start` launches nvim (found by the
    battery check 9 e2e, 2026-09-29)."""
    path = launch_socket_path(window_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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
    sock = str(_socket_path_in_existing_dir(window_id))
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


def _answers(sock: str) -> bool:
    """True when something ACCEPTS a connection on the socket. Existence is
    not readiness: a SIGKILLed nvim leaves its socket file behind, and a
    launcher that started nothing then looked like an attached follower."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(_SOCKET_POLL_INTERVAL_SECONDS * 10)
    try:
        probe.connect(sock)
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _remove_dead_socket(sock: str) -> None:
    """Clear a socket file nobody answers on, so the new nvim can bind the
    path and a leftover can never pass for it. One that answers is a live
    nvim's and is never removed."""
    path = Path(sock)
    if path.exists() and not _answers(sock):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # a directory there, or a file we may not delete
            raise SocketPathBlocked(sock, exc) from exc


def launch_backoff_path(window_id: str) -> Path:
    return cache.CACHE_DIR / f"{window_id}.launch-failed"


def record_launch_failure(window_id: str) -> None:
    path = launch_backoff_path(window_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()  # the mtime is the failure's time


def launch_backoff_active(window_id: str) -> bool:
    try:
        failed_at = launch_backoff_path(window_id).stat().st_mtime
    except FileNotFoundError:
        return False
    return time.time() - failed_at < LAUNCH_BACKOFF_SECONDS


def clear_launch_backoff(window_id: str) -> None:
    launch_backoff_path(window_id).unlink(missing_ok=True)


def answering_socket(window_id: str) -> str | None:
    """This window's standalone socket when an nvim already answers on it —
    typically one a slow launcher bound after the caller stopped waiting."""
    sock = str(launch_socket_path(window_id))
    return sock if _answers(sock) else None


def launch_standalone_nvim(
    window_id: str, wait_seconds: float = _STANDALONE_SOCKET_WAIT_SECONDS
) -> str:
    sock = str(_socket_path_in_existing_dir(window_id))
    if _answers(sock):
        # Already up (a launch that outlived its caller's wait): attach. A
        # second launch's nvim would die on "address already in use" and
        # stay on screen as an orphan error window.
        return sock
    _remove_dead_socket(sock)
    cmd = standalone_launch_command(
        sock,
        has_nvim_qt=shutil.which("nvim-qt") is not None,
        has_vimr=shutil.which("VimR") is not None or _vimr_app_present(),
        is_iterm=os.environ.get("TERM_PROGRAM") == "iTerm.app",
    )
    subprocess.run(cmd, check=True)
    deadline = time.monotonic() + wait_seconds
    while not _answers(sock) and time.monotonic() < deadline:
        time.sleep(_SOCKET_POLL_INTERVAL_SECONDS)
    if not _answers(sock):
        # The launcher exited 0 (nvim-qt that found no nvim, a terminal whose
        # nvim died on a config error): there is nothing to attach to, and a
        # follower persisted here would point at a socket no one listens on.
        raise NvimNeverListened(sock, cmd[0], wait_seconds)
    return sock
