"""A standalone nvim counts as launched only once its init has run.

B2 (T19 residual): `launch_standalone_nvim` and `answering_socket` took one
get_mode answer as "ready", and a TUI nvim gives that answer for a few ms
after it binds its socket, before its init runs (measured on the tmux split:
1 launch in 8). The terminal app the launcher opens is replaced here by a
window of a private tmux server running the same `nvim --listen <socket>`;
the init sleeps, then sets `g:init_done`, and right after the launch returns
the nvim must already have entered (`v:vim_did_enter`, asked with a short
bound: a request sent while the init sleeps waits for it).
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from vim_ai_follower import cache
from vim_ai_follower.backends import nvim_connect

pytestmark = pytest.mark.integration

LAUNCHES = 10
_SLOW_INIT = "set noloadplugins shada= | lua vim.uv.sleep(300) vim.g.init_done = 1"
_ERROR_AT_STARTUP = 'set noloadplugins shada= | echoerr "boom from the init"'


@pytest.fixture
def short_cache_directory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The socket lives in the cache dir, and an AF_UNIX path must fit in
    ~104 bytes on macOS: pytest's tmp_path does not."""
    directory = Path(tempfile.mkdtemp(prefix="vaf14s", dir="/tmp"))
    monkeypatch.setattr(cache, "CACHE_DIRECTORY", directory)
    try:
        yield directory
    finally:
        found = subprocess.run(
            ["pgrep", "-f", str(directory)], capture_output=True, text=True, check=False
        )
        for pid in found.stdout.split():
            os.kill(int(pid), signal.SIGKILL)
        shutil.rmtree(directory, ignore_errors=True)


def _launch_in_a_tmux_window(monkeypatch: pytest.MonkeyPatch, viminit: str) -> None:
    def launch_command(sock: str, **_detected: bool) -> list[str]:
        nvim = shlex.join(["env", f"VIMINIT={viminit}", "nvim", "--listen", sock])
        return ["tmux", "new-window", "-d", nvim]

    monkeypatch.setattr(nvim_connect, "standalone_launch_command", launch_command)


def test_a_standalone_nvim_is_ready_only_after_its_init(
    tmux_session: str, short_cache_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _launch_in_a_tmux_window(monkeypatch, _SLOW_INIT)
    early = []
    for launch in range(LAUNCHES):
        window_id = f"@{900 + launch}"
        sock = nvim_connect.launch_standalone_nvim(window_id, wait_seconds=10.0)
        if not nvim_connect._did_enter(sock, 0.1):
            early.append(launch)
        subprocess.run(["pkill", "-KILL", "-f", f"--listen {sock}"], check=False)
    assert early == [], f"launches {early} of {LAUNCHES} returned before the init ran"


def test_a_standalone_nvim_at_its_startup_prompt_is_not_started(
    tmux_session: str, short_cache_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _launch_in_a_tmux_window(monkeypatch, _ERROR_AT_STARTUP)
    with pytest.raises(nvim_connect.SplitNvimNotStarted, match="press Enter there or close"):
        nvim_connect.launch_standalone_nvim("@950", wait_seconds=3.0)
    sock = str(nvim_connect.launch_socket_path("@950"))
    assert nvim_connect.answering_socket("@950") is None
    assert nvim_connect.nvim_answers(sock)  # it is up, at its prompt: not ready
