from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from helpers import PATH_HANDLES, resolve_typed_paths

from vim_ai_follower import cache, config, hooks, session, snapshot
from vim_ai_follower.backends import nvim_connect, tmux_vim
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
    # The readonly restores this process sent, by pane (tmux_vim): a token
    # one test sent must never be awaited by the next.
    monkeypatch.setattr(tmux_vim, "_SENT_READONLY_TOKENS", {}, raising=False)
    # The probe verdicts this process remembers, by socket: a "silent" one
    # test left behind must never make the next one's follower read as busy.
    monkeypatch.setattr(nvim_connect, "_last_verdicts", {})
    monkeypatch.setattr(nvim_connect, "_last_blocking", {})
    monkeypatch.setattr(nvim_connect, "_last_modes", {})
    yield
    hooks.logger.handlers.clear()


@pytest.fixture(scope="session")
def no_tmux_server_dir() -> Iterator[str]:
    """A short private directory (a tmux socket path must fit sun_path, so
    not pytest's long tmp_path), removed when the run ends: created at
    import, one was left behind by every pytest run."""
    directory = tempfile.mkdtemp(prefix="cf-none-")
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def no_real_tmux_server(monkeypatch: pytest.MonkeyPatch, no_tmux_server_dir: str) -> None:
    """An unmocked in-process tmux call reaches no server at all. pytest run
    from a tmux pane inherits $TMUX, and code that asks the server for its
    pid (binding.tmux_server_pid, which FollowerState.set calls to record
    it) then talked to the developer's REAL server: a read, but enough to
    make a state's recorded pid depend on where the suite ran, while CI has
    no server. TMUX_TMPDIR at an empty directory makes the default socket
    one nobody listens on. Integration tests too: tmux_session (requested
    after this autouse fixture) points the process at its private server,
    and the e2e harness isolates only its subprocesses, so an in-process
    FollowerState.set there must name world.server_pid itself."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", no_tmux_server_dir)


@pytest.fixture(autouse=True)
def record_path_handles(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remember every path handle the tmux backend creates, by name, for
    helpers.resolve_typed_paths: the sent line names only the handle."""
    create = tmux_vim._path_handle

    def recorded(pane_id: str, file_path: str) -> Path:
        handle = create(pane_id, file_path)
        PATH_HANDLES[handle.name] = handle
        return handle

    monkeypatch.setattr(tmux_vim, "_path_handle", recorded)
    yield
    PATH_HANDLES.clear()


@pytest.fixture(autouse=True)
def vim_confirms_landing(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tmux backend waits for its Vim to confirm that a navigation landed
    (tmux_vim._await_landing) and refuses to go on otherwise. A unit test's
    Vim is a mocked subprocess that never answers, so the confirmation is
    taken as given here; integration tests, whose Vim is real, and tests
    marked @pytest.mark.real_landing keep the real wait."""
    if request.node.get_closest_marker("integration") or request.node.get_closest_marker(
        "real_landing"
    ):
        return
    monkeypatch.setattr(tmux_vim, "_await_landing", lambda *_args: None)


@pytest.fixture(autouse=True)
def format_reads_under_tmp_path(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit tests drive the nvim backend with a mock nvim and fake paths
    (`/tmp/f.py`), and a completed animation reads the file for 'bomb' and
    'fileformat' (_format_from_disk). Re-root that read under this test's tmp_path,
    where the fake path does not exist, so no unit test depends on what the
    machine's real /tmp holds. Integration tests read their real files.
    Without pynvim (the optional `nvim` extra) there is no nvim backend to
    re-root, and every other unit test must still run."""
    if "integration" in request.keywords:
        return
    try:
        from vim_ai_follower.backends import nvim as nvim_backend
    except ImportError:
        return

    real = nvim_backend._format_from_disk

    def under_tmp_path(nvim: Any, buf: int, file_path: str) -> None:
        real(nvim, buf, str(tmp_path / "disk" / file_path.lstrip("/")))

    monkeypatch.setattr(nvim_backend, "_format_from_disk", under_tmp_path)


@pytest.fixture(autouse=True)
def no_pid_ancestry_walk(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch off resolve_session's pid-ancestry fallback for the whole suite.

    That fallback asks the REAL tmux server about the REAL process tree, and a
    pytest process started from a tmux pane is itself a pane's descendant —
    verified on this machine (2026-09-22): the runner's ancestry reaches the
    pid tmux lists for pane %12. Left alone, every `resolve_session({})` in the
    suite would resolve to whatever window the developer happens to be sitting
    in, while CI (no tmux server) kept getting the synthetic identity: green
    here, green there, and neither one measuring the same thing.

    Tests that mean to exercise the walk opt back in with
    @pytest.mark.pid_walk, and supply their own fake (or, for the one
    integration test, a throwaway server)."""
    if "pid_walk" in request.keywords:
        return
    monkeypatch.setattr(session, "_tmux_panes_by_pid", dict)


@pytest.fixture(scope="session", autouse=True)
def no_e2e_process_survives() -> Iterator[None]:
    """Once the session is over, no process of an e2e world may be alive: the
    harness's teardown kills what it spawned, and this is what notices when
    it does not (a setup error that skipped the teardown, an editor that
    outlived `kill-server`). Only asserts when an e2e world was built, so a
    run that touched none cannot trip over another suite's processes."""
    yield
    harness = sys.modules.get("e2e_harness")
    if harness is None or not harness.worlds_started:
        return
    survivors = subprocess.run(
        ["pgrep", "-fl", "vaf-e2e-"], capture_output=True, text=True, check=False
    ).stdout.strip()
    assert survivors == "", f"e2e processes outlived the suite:\n{survivors}"


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
def headless_nvim() -> Iterator[str]:
    """A throwaway headless Neovim listening on a private socket, parallel to
    tmux_session. `-n` (no swap), `-u NONE -i NONE` (no vimrc/shada) keep it
    hermetic — no user plugins or config leak into the animation. The socket
    file appears within ~1s of launch; poll for it before yielding."""
    socket_dir = tempfile.mkdtemp(prefix="cf-nvim-")
    sock = str(Path(socket_dir) / "nvim.sock")
    proc = subprocess.Popen(
        ["nvim", "--headless", "-n", "-u", "NONE", "-i", "NONE", "--listen", sock]
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not Path(sock).exists():
        time.sleep(0.05)
    try:
        yield sock
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive cleanup
            proc.kill()
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
    follower_instance = TmuxVimFollower(pane_id="%2", window_id="@1")

    def capture_run(cmd: list[str], **kwargs: str) -> MagicMock:
        """Mock subprocess.run that captures tmux send-keys commands."""
        if cmd[:4] == ["tmux", "send-keys", "-t", "%2"]:
            if "-l" in cmd:
                # send_text: ["tmux", "send-keys", "-t", "%2", "-l", "--", text]
                sent.append(f"text::{resolve_typed_paths(cmd[6])}")
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
