from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from vim_ai_follower import cache, cli, snapshot


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Redirect every on-disk location (cache dir shared by state, signals,
    pending files and saved keybindings; snapshots; hook log) to a per-test
    tmp dir. cache.CACHE_DIR is read at call time by its consumers, so this
    single setattr isolates all of them."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(snapshot, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "hook.log")
    yield
    cli.logger.handlers.clear()


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
    # Keep the integration vims hermetic: VIMINIT replaces the user's vimrc,
    # so no Copilot/CoC/LSP plugins boot inside every test vim. Plugins react
    # to the animated keystrokes nondeterministically (completion popups can
    # swallow keys, ESC timing shifts), and copilot-language-server triggers
    # macOS keychain password prompts on every suite run.
    # noloadplugins also blocks native packages (~/.vim/pack/*/start/*),
    # which load even when the vimrc is skipped.
    monkeypatch.setenv("VIMINIT", "set nocompatible noloadplugins")
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
