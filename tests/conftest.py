from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower import cache, config, hooks, snapshot
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Redirect every on-disk location (cache dir shared by state, signals,
    pending files and saved keybindings; snapshots; hook log) to a per-test
    tmp dir. cache.CACHE_DIR is read at call time by its consumers, so this
    single setattr isolates all of them."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(snapshot, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(hooks, "LOG_PATH", tmp_path / "hook.log")
    # The user's real ~/.config/claude-vim-follower/config.json must never
    # leak into tests (a live open_policy=always there silently flipped
    # auto-open tests). Point at a nonexistent per-test path: defaults.
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    yield
    hooks.logger.handlers.clear()


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


@pytest.fixture
def sent() -> list[str]:
    """List of sent commands in format 'text::...' or 'key::...'. A Vim
    ex-command brings its own leading ':', so it records with THREE colons
    ('text:::tabnew') — assert against that form, never 'text::tabnew'."""
    return []


@pytest.fixture
def follower(sent: list[str], tmp_path: Path) -> Iterator[TmuxVimFollower]:
    """TmuxVimFollower with mocked subprocess to capture sent commands.

    Note: test_tmux_vim.py has its own capture double (_sent_commands, which
    reads a MagicMock's call_args_list after the fact into (text, literal)
    pairs). The two are kept separate on purpose: this fixture streams into a
    live list during the run (needed by tests that assert ordering across an
    interrupt), while _sent_commands is a post-hoc reader for the pure
    keystroke-sequence assertions. Consolidating would force one style onto
    both, so they stay split."""
    follower_instance = TmuxVimFollower(pane_id="%2", session_id="$1")

    def capture_run(cmd: list[str], **kwargs: str) -> MagicMock:
        """Mock subprocess.run that captures tmux send-keys commands."""
        if cmd[:4] == ["tmux", "send-keys", "-t", "%2"]:
            if "-l" in cmd:
                # send_text: ["tmux", "send-keys", "-t", "%2", "-l", "--", text]
                sent.append(f"text::{cmd[6]}")
            else:
                # send_key: ["tmux", "send-keys", "-t", "%2", key_name]
                sent.append(f"key::{cmd[4]}")
        mock = MagicMock()
        mock.stdout = "%1 zsh\n%2 vim\n"
        return mock

    run_patcher = patch("vim_ai_follower.tmux.subprocess.run", side_effect=capture_run)
    run_patcher.start()

    # Also patch cache.CACHE_DIR which is used by the follower
    cache_patcher = patch("vim_ai_follower.cache.CACHE_DIR", tmp_path / "cache")
    cache_patcher.start()

    # Patch control.check_signal to return None (no interruptions)
    signal_patcher = patch("vim_ai_follower.control.check_signal", return_value=None)
    signal_patcher.start()

    yield follower_instance

    run_patcher.stop()
    cache_patcher.stop()
    signal_patcher.stop()
