"""A tmux split whose nvim never answers RPC (T19, Alberto's answer B).

A real private tmux server splits a real `nvim --listen <socket>` whose init
spins forever (VIMINIT `lua while true do end`): the socket file appears at
once and nobody ever answers on it, which is exactly what the old
"the socket file exists" readiness check reported as attached. Measured
2026-10-17: an init that only ERRORS (`echoerr` at startup) is not this case,
since nvim answers get_mode at that hit-enter prompt.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from vim_ai_follower import cache, commands, config, hooks, state
from vim_ai_follower.backends import nvim_connect
from vim_ai_follower.session import Session

pytestmark = pytest.mark.integration

_SPIN_FOREVER = "lua while true do end"
# No plugins and no shada (nothing of the user's is read or written), then
# the error: nvim stops at a startup hit-enter prompt showing it.
_ERROR_AT_STARTUP = 'set noloadplugins shada= | echoerr "boom from the init"'


@pytest.fixture
def short_cache_directory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The socket lives in the cache dir, and an AF_UNIX path must fit in
    ~104 bytes on macOS: pytest's tmp_path does not."""
    directory = Path(tempfile.mkdtemp(prefix="vaf19i", dir="/tmp"))
    monkeypatch.setattr(cache, "CACHE_DIRECTORY", directory)
    try:
        yield directory
    finally:
        # A spinning nvim never runs its signal handlers: SIGHUP from the
        # pane closing and SIGTERM leave its --embed server alive.
        found = subprocess.run(
            ["pgrep", "-f", str(directory)], capture_output=True, text=True, check=False
        )
        for pid in found.stdout.split():
            os.kill(int(pid), signal.SIGKILL)
        shutil.rmtree(directory, ignore_errors=True)


def _panes(session_name: str) -> list[tuple[str, str]]:
    listed = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_id} #{pane_current_command}"],
        capture_output=True,
        text=True,
        check=True,
    )
    panes = []
    for line in listed.stdout.splitlines():
        pane_id, _, command = line.partition(" ")
        panes.append((pane_id, command))
    return panes


def _spinning_nvim(socket_path: Path) -> list[str]:
    found = subprocess.run(
        ["pgrep", "-f", "--", f"--listen {socket_path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return found.stdout.split()


def _window_whose_splits_run(
    session_name: str, viminit: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str]:
    """(origin pane, window id) of the private server's only window, whose
    new panes' nvims run `viminit` instead of any user config."""
    socket_path = subprocess.run(
        ["tmux", "display-message", "-p", "#{socket_path}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert socket_path.startswith(os.path.realpath(os.environ["TMUX_TMPDIR"]))  # private
    subprocess.run(["tmux", "set-environment", "-g", "VIMINIT", viminit], check=True)
    [(origin, _command)] = _panes(session_name)
    window_id = subprocess.run(
        ["tmux", "display-message", "-p", "-t", origin, "#{window_id}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    return origin, window_id


def test_a_split_nvim_that_never_answers_is_reported_left_open_and_backed_off(
    tmux_session: str,
    short_cache_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="vim_ai_follower")
    origin, window_id = _window_whose_splits_run(tmux_session, _SPIN_FOREVER, tmp_path, monkeypatch)
    nvim_socket = state.nvim_socket_path(window_id)
    in_tmux = Session(window_id=window_id, origin=origin, in_tmux=True)

    # The hook's auto-open: 3 s, then the failure, the pane left open.
    assert hooks._maybe_auto_open(in_tmux, "/tmp/f.txt", config.load()) is None
    message = (
        f"nvim did not answer on {nvim_socket} within 3 s; "
        "its pane is left open so you can read why"
    )
    assert capsys.readouterr().err == f"claude-follow: {message}\n"
    assert state.FollowerState.read(window_id) is None
    panes = _panes(tmux_session)
    assert len(panes) == 2 and panes[1][1] == "nvim", panes
    assert nvim_socket.is_socket()  # bound, never answering: the old "attached"
    assert nvim_connect.launch_backoff_active(window_id)
    assert caplog.text.count(message) == 1

    # The next hook, within the backoff, splits nothing.
    assert hooks._maybe_auto_open(in_tmux, "/tmp/g.txt", config.load()) is None
    assert len(_panes(tmux_session)) == 2

    # The spinning nvim dies hard and leaves its socket file; `start`, the
    # user's retry, splits a new pane at once despite the backoff and waits
    # its own 10 s — and still never says "attached".
    for pid in _spinning_nvim(nvim_socket):
        os.kill(int(pid), signal.SIGKILL)
    deadline = time.monotonic() + 5.0
    while len(_panes(tmux_session)) > 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(_panes(tmux_session)) == 1
    started = time.monotonic()
    assert commands.cmd_start({"TMUX_PANE": origin}, backend="nvim") == 1
    assert time.monotonic() - started >= nvim_connect.START_SPLIT_WAIT_SECONDS
    captured = capsys.readouterr()
    assert "attached" not in captured.out
    assert captured.err == (
        f"claude-follow: nvim did not answer on {nvim_socket} within 10 s; "
        "its pane is left open so you can read why\n"
    )
    assert state.FollowerState.read(window_id) is None
    panes = _panes(tmux_session)
    assert len(panes) == 2 and panes[1][1] == "nvim", panes


def test_an_init_error_stays_on_screen_and_the_split_is_attached_once_nvim_starts(
    tmux_session: str,
    short_cache_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An nvim at its startup hit-enter prompt answers get_mode: the old check
    # and a bare nvim_answers both took it for a ready follower, whose prompt
    # watchdog would then sweep the error away unread.
    origin, window_id = _window_whose_splits_run(
        tmux_session, _ERROR_AT_STARTUP, tmp_path, monkeypatch
    )
    nvim_socket = state.nvim_socket_path(window_id)
    in_tmux = Session(window_id=window_id, origin=origin, in_tmux=True)

    assert hooks._maybe_auto_open(in_tmux, "/tmp/f.txt", config.load()) is None
    assert "its pane is left open so you can read why" in capsys.readouterr().err
    assert state.FollowerState.read(window_id) is None
    panes = _panes(tmux_session)
    assert len(panes) == 2 and panes[1][1] == "nvim", panes
    error_pane = panes[1][0]
    screen = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", error_pane], capture_output=True, text=True, check=True
    ).stdout
    assert "boom from the init" in screen

    # The user reads it and presses Enter: nvim finishes starting, and the
    # next hook attaches to THAT pane despite the backoff, splitting nothing.
    subprocess.run(["tmux", "send-keys", "-t", error_pane, "Enter"], check=True)
    deadline = time.monotonic() + 5.0
    while not nvim_connect.nvim_ready(str(nvim_socket)) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert nvim_connect.launch_backoff_active(window_id)
    attached = hooks._maybe_auto_open(in_tmux, "/tmp/g.txt", config.load())
    assert attached is not None
    assert attached.target == str(nvim_socket)
    assert attached.adopted is False
    assert [pane_id for pane_id, _command in _panes(tmux_session)] == [origin, error_pane]
    assert not nvim_connect.launch_backoff_active(window_id)


def _fill_the_listen_queue(sock: Path) -> None:
    """What ~32 hooks' probes leave on an nvim that never accepts: on macOS a
    connection its client closed keeps its slot, and nvim's backlog is 32
    (review I1, measured 2026-10-17). Past it, connect is refused."""
    for _attempt in range(40):
        nvim_connect.probe_nvim(str(sock), timeout=0.05)


def test_a_spinning_nvims_full_listen_queue_never_reads_as_a_dead_socket(
    short_cache_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = short_cache_directory / "nvim-@91.sock"
    spinner = subprocess.Popen(
        ["nvim", "--headless", "-u", "NONE", "-i", "NONE", "-n", "--listen", str(sock),
         "--cmd", _SPIN_FOREVER],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 5.0
        while not sock.is_socket() and time.monotonic() < deadline:
            time.sleep(0.02)
        _fill_the_listen_queue(sock)
        refused = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with pytest.raises(ConnectionRefusedError):  # the queue IS full
            refused.connect(str(sock))
        refused.close()
        assert nvim_connect.probe_nvim(str(sock)) == "silent"
        with pytest.raises(nvim_connect.SocketPathBlocked):
            nvim_connect._remove_dead_socket(str(sock))
        then = time.time() - nvim_connect.SOCKET_SWEEP_GRACE_SECONDS - 5
        os.utime(sock, (then, then))
        assert nvim_connect.sweep_stale_sockets() == []
        assert sock.is_socket()
        assert spinner.poll() is None
    finally:
        spinner.kill()
        spinner.wait()


def test_a_failed_split_whose_queue_filled_is_never_split_beside(
    tmux_session: str,
    short_cache_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    origin, window_id = _window_whose_splits_run(tmux_session, _SPIN_FOREVER, tmp_path, monkeypatch)
    nvim_socket = state.nvim_socket_path(window_id)
    in_tmux = Session(window_id=window_id, origin=origin, in_tmux=True)
    assert hooks._maybe_auto_open(in_tmux, "/tmp/f.txt", config.load()) is None
    spinning = _spinning_nvim(nvim_socket)
    assert spinning
    _fill_the_listen_queue(nvim_socket)  # ~32 hooks during the backoff
    nvim_connect.clear_launch_backoff(window_id)  # the 5 minutes passed
    capsys.readouterr()

    assert hooks._maybe_auto_open(in_tmux, "/tmp/g.txt", config.load()) is None
    assert len(_panes(tmux_session)) == 2, "a second nvim was split beside the first"
    assert nvim_socket.is_socket()
    assert _spinning_nvim(nvim_socket) == spinning
    assert nvim_connect.launch_backoff_active(window_id)
    assert f"cannot clear {nvim_socket}" in capsys.readouterr().err


def test_a_stopped_adopted_nvims_full_queue_still_reads_alive(tmp_path: Path) -> None:
    """An adopted nvim's socket is nvim's default one (`$TMPDIR/nvim.$USER/
    <random>/nvim.<pid>.0`, what discover_adopt_socket globs) and carries no
    `--listen` argument. SIGSTOPped (Ctrl-Z in the user's pane), it refuses
    connects after 32 probes; it must stay "silent" and alive, or another
    window's `stop` or hook would collect a live follower's state."""
    from vim_ai_follower.backends.nvim import NvimFollower

    directory = Path(tempfile.mkdtemp(prefix="vaf19a", dir="/tmp"))
    name_file = directory / "servername"
    nvim = subprocess.Popen(
        ["nvim", "--headless", "-u", "NONE", "-i", "NONE", "-n",
         "--cmd", f"call writefile([v:servername], '{name_file}')"],
        env={**os.environ, "TMPDIR": f"{directory}/"},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 5.0
        while not name_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        sock = Path(name_file.read_text().strip())
        assert sock.name == f"nvim.{nvim.pid}.0"
        assert sock.parent.parent.name.startswith("nvim.")
        os.kill(nvim.pid, signal.SIGSTOP)
        _fill_the_listen_queue(sock)
        refused = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with pytest.raises(ConnectionRefusedError):
            refused.connect(str(sock))
        refused.close()
        assert nvim_connect.probe_nvim(str(sock)) == "silent"
        assert NvimFollower(socket_path=str(sock)).is_alive()
        assert sock.is_socket()
    finally:
        os.kill(nvim.pid, signal.SIGCONT)
        nvim.kill()
        nvim.wait()
        shutil.rmtree(directory, ignore_errors=True)
