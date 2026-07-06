from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator

import pytest


@pytest.fixture
def tmux_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    # Isolate from the real tmux server the test runner itself may be
    # attached to: unset $TMUX (which would otherwise take priority and
    # route every bare `tmux` call — including the ones inside cli.py's own
    # subprocess calls — straight to that real, live server) and point
    # TMUX_TMPDIR at a private directory, so the default socket resolves to
    # a brand-new, throwaway server instead. Uses tempfile directly (not the
    # tmp_path fixture) because tmux's socket path has to stay under the
    # ~104-char Unix domain socket limit, and pytest's per-test tmp_path is
    # nested too deeply for that.
    monkeypatch.delenv("TMUX", raising=False)
    socket_dir = tempfile.mkdtemp(prefix="cf-tmux-")
    monkeypatch.setenv("TMUX_TMPDIR", socket_dir)
    session_name = f"pytest-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-x", "100", "-y", "30"],
        check=True,
    )
    try:
        yield session_name
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=False)
        subprocess.run(["tmux", "kill-server"], check=False)
        shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.fixture
def wait_until() -> Callable[..., bool]:
    def _wait(predicate: Callable[[], bool], timeout: float = 3.0, interval: float = 0.1) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    return _wait
