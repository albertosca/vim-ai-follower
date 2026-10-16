from __future__ import annotations

import os
import shutil
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from helpers import fake_nvim

from vim_ai_follower import cache
from vim_ai_follower.backends import nvim_connect
from vim_ai_follower.state import FollowerState

# How long a test whose deadline went missing may block before its guard
# unblocks it, so the regression FAILS in bounded time (CI has no
# pytest-timeout).
_HANG_GUARD_SECONDS = 5.0


def test_discover_adopt_socket_matches_the_panes_nvim_pid(tmp_path: Path) -> None:
    # simulate $TMPDIR/nvim.$USER/<rand>/nvim.<pid>.0 with a matching + a stale one
    (tmp_path / "nvim.me" / "AAA").mkdir(parents=True)
    (tmp_path / "nvim.me" / "BBB").mkdir(parents=True)
    match = tmp_path / "nvim.me" / "AAA" / "nvim.4242.0"
    match.write_text("")
    (tmp_path / "nvim.me" / "BBB" / "nvim.9999.0").write_text("")  # stale, other pid
    with (
        patch.dict("os.environ", {"TMPDIR": str(tmp_path), "USER": "me"}, clear=False),
        patch("vim_ai_follower.backends.nvim_connect.TmuxPane") as TP,
    ):
        TP.return_value.pane_pid.return_value = 4242
        assert nvim_connect.discover_adopt_socket("%3") == str(match)


def test_discover_adopt_socket_none_when_no_match(tmp_path: Path) -> None:
    with (
        patch.dict("os.environ", {"TMPDIR": str(tmp_path), "USER": "me"}, clear=False),
        patch("vim_ai_follower.backends.nvim_connect.TmuxPane") as TP,
    ):
        TP.return_value.pane_pid.return_value = 4242
        assert nvim_connect.discover_adopt_socket("%3") is None


def test_discover_adopt_socket_none_when_pane_has_no_pid(tmp_path: Path) -> None:
    with (
        patch.dict("os.environ", {"TMPDIR": str(tmp_path), "USER": "me"}, clear=False),
        patch("vim_ai_follower.backends.nvim_connect.TmuxPane") as TP,
    ):
        TP.return_value.pane_pid.return_value = None
        assert nvim_connect.discover_adopt_socket("%3") is None


def test_resolve_launches_when_adopt_requested_but_nothing_found(tmp_path: Path) -> None:
    # Pre-create the socket file so the post-launch readiness poll sees it
    # immediately (as if nvim had already bound it) instead of burning the
    # real bounded wait — subprocess.run is mocked and never actually starts
    # nvim, so nothing else would ever make the file appear.
    (tmp_path / "nvim-@1.sock").write_text("")
    with (
        patch("vim_ai_follower.backends.nvim_connect.discover_adopt_socket", return_value=None),
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=tmp_path / "nvim-@1.sock",
        ),
        patch("vim_ai_follower.backends.nvim_connect.subprocess.run") as run,
    ):
        sock, launched = nvim_connect.resolve_nvim_target("%1", "@1", adopt=True)
    assert (sock, launched) == (str(tmp_path / "nvim-@1.sock"), True)
    # adopt requested but nothing found -> still splits a pane running nvim --listen
    args = run.call_args_list[0].args[0]
    assert args[:3] == ["tmux", "split-window", "-h"] and "nvim" in args and "--listen" in args


def test_resolve_adopts_when_socket_found() -> None:
    with (
        patch(
            "vim_ai_follower.backends.nvim_connect.discover_adopt_socket", return_value="/s.sock"
        ),
    ):
        assert nvim_connect.resolve_nvim_target("%1", "@1", adopt=True) == ("/s.sock", False)


def test_resolve_launches_when_no_adopt(tmp_path: Path) -> None:
    (tmp_path / "nvim-@1.sock").write_text("")
    with (
        patch("vim_ai_follower.backends.nvim_connect.discover_adopt_socket", return_value=None),
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=tmp_path / "nvim-@1.sock",
        ),
        patch("vim_ai_follower.backends.nvim_connect.subprocess.run") as run,
    ):
        sock, launched = nvim_connect.resolve_nvim_target("%1", "@1", adopt=False)
    assert (sock, launched) == (str(tmp_path / "nvim-@1.sock"), True)
    # split a pane running `nvim --listen <sock>`
    args = run.call_args_list[0].args[0]
    assert args[:3] == ["tmux", "split-window", "-h"] and "nvim" in args and "--listen" in args


def test_resolve_launched_waits_for_socket_to_appear(tmp_path: Path) -> None:
    """The launched branch must not return before the socket file exists —
    otherwise the caller's immediate pynvim.attach() races nvim's own
    startup and raises OSError (the auto-open bug this guards against)."""
    sock_path = tmp_path / "nvim-@1.sock"
    poll_count = 0

    class _FakePath:
        """Stand-in for the Path(sock) the poll loop constructs each
        iteration — real nvim wouldn't create the socket file synchronously
        inside the (mocked) subprocess.run call, so real Path.exists() would
        just be False the whole time and prove nothing about waiting."""

        def __init__(self, raw: str) -> None:
            self._raw = raw

        def exists(self) -> bool:
            nonlocal poll_count
            poll_count += 1
            if poll_count >= 3:
                sock_path.write_text("")
                return True
            return False

        def __str__(self) -> str:
            return self._raw

    with (
        patch("vim_ai_follower.backends.nvim_connect.discover_adopt_socket", return_value=None),
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=sock_path,
        ),
        patch("vim_ai_follower.backends.nvim_connect.subprocess.run"),
        patch("vim_ai_follower.backends.nvim_connect.time.sleep") as fake_sleep,
        patch("vim_ai_follower.backends.nvim_connect.Path", side_effect=_FakePath),
    ):
        sock, launched = nvim_connect.resolve_nvim_target("%1", "@1", adopt=False)
    assert (sock, launched) == (str(sock_path), True)
    assert poll_count >= 3
    fake_sleep.assert_called()  # it actually waited, not just checked once


def test_standalone_command_prefers_nvim_qt() -> None:
    cmd = nvim_connect.standalone_launch_command(
        "/s.sock", has_nvim_qt=True, has_vimr=True, is_iterm=True
    )
    assert cmd == ["nvim-qt", "--", "--listen", "/s.sock"]


def test_standalone_command_uses_vimr_when_no_nvim_qt() -> None:
    cmd = nvim_connect.standalone_launch_command(
        "/s.sock", has_nvim_qt=False, has_vimr=True, is_iterm=True
    )
    assert cmd[:3] == ["open", "-a", "VimR"]  # + the file/socket wiring


def test_standalone_command_uses_iterm_split_when_no_gui_app() -> None:
    cmd = nvim_connect.standalone_launch_command(
        "/s.sock", has_nvim_qt=False, has_vimr=False, is_iterm=True
    )
    assert cmd[0] == "osascript"
    script = cmd[2]
    # bundle id, not display name — immune to a future app rename
    assert 'application id "com.googlecode.iterm2"' in script
    assert "current session of current window" in script
    assert "split vertically with default profile" in script
    assert "nvim --listen /s.sock" in script


def test_standalone_command_falls_back_to_terminal_app_when_not_iterm() -> None:
    cmd = nvim_connect.standalone_launch_command(
        "/s.sock", has_nvim_qt=False, has_vimr=False, is_iterm=False
    )
    assert cmd[0] == "osascript"
    assert any("nvim --listen /s.sock" in part for part in cmd)
    # `do script` alone can create an invisible, backgrounded window when
    # Terminal.app is already running (live finding, 2026-09-03: two windows
    # created by `do script` both came back `visible=false` with Terminal not
    # frontmost — nothing appeared on screen even though the command
    # "succeeded"). `activate` is what makes the window actually show up.
    assert any("activate" in part for part in cmd)


def test_launch_standalone_nvim_raises_when_no_nvim_ever_listens(
    tmp_path: Path,
) -> None:
    """A launcher that exits 0 and starts nothing (an nvim-qt that cannot find
    nvim, a Terminal window whose nvim dies on a config error) used to hand
    back the socket anyway, and `start` said "attached" to nothing. Past the
    deadline it is an error, naming the launcher and the socket. The deadline
    is a GUI app's cold launch, longer than a tmux split's."""
    sock_path = tmp_path / "nvim-@1.sock"  # deliberately never created
    with (
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=sock_path,
        ),
        patch("vim_ai_follower.backends.nvim_connect.subprocess.run"),
        patch("vim_ai_follower.backends.nvim_connect.time.sleep") as fake_sleep,
        patch(
            "vim_ai_follower.backends.nvim_connect.time.monotonic",
            side_effect=[0.0, 9.9, 10.1],
        ),
        patch(
            "vim_ai_follower.backends.nvim_connect.shutil.which", return_value="/usr/bin/nvim-qt"
        ),
        patch("vim_ai_follower.backends.nvim_connect._vimr_app_present", return_value=False),
        pytest.raises(nvim_connect.NvimNeverListened) as raised,
    ):
        nvim_connect.launch_standalone_nvim("@1")
    assert str(raised.value) == f"nvim-qt exited but no nvim listened on {sock_path} within 10 s"
    assert raised.value.launcher == "nvim-qt"
    assert fake_sleep.call_count == 1  # still waiting at 9.9 s, gave up past 10 s
    assert not sock_path.exists()


@contextmanager
def _short_socket_dir() -> Iterator[Path]:
    """A unix socket path must fit in ~104 bytes on macOS; pytest's tmp_path
    does not, so sockets live in a short /tmp directory, removed after."""
    directory = Path(tempfile.mkdtemp(prefix="vafs", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _dead_nvims_socket(path: Path) -> None:
    """What a SIGKILLed nvim leaves: the socket file, with nobody listening."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.close()  # closed without unlinking


def _patched_launcher(sock_path: Path, run: Any) -> AbstractContextManager[Any]:
    stack = ExitStack()
    stack.enter_context(
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=sock_path,
        )
    )
    stack.enter_context(patch("vim_ai_follower.backends.nvim_connect.subprocess.run", run))
    stack.enter_context(
        patch("vim_ai_follower.backends.nvim_connect.shutil.which", return_value="/usr/bin/nvim-qt")
    )
    stack.enter_context(
        patch("vim_ai_follower.backends.nvim_connect._vimr_app_present", return_value=False)
    )
    return stack


def test_launch_standalone_nvim_is_not_fooled_by_a_dead_nvims_socket_file() -> None:
    # A SIGKILLed nvim leaves its socket file. Existence as readiness made a
    # launcher that started nothing look like success: "attached", rc 0, a
    # follower on a socket nobody answers. The file is cleared before the
    # launch (so a real nvim can bind it) and readiness means a connection
    # is accepted.
    with _short_socket_dir() as directory:
        sock_path = directory / "nvim-@1.sock"
        _dead_nvims_socket(sock_path)
        present_at_launch: list[bool] = []

        def launcher_starting_nothing(*_args: object, **_kwargs: object) -> None:
            present_at_launch.append(sock_path.exists())

        with (
            _patched_launcher(sock_path, launcher_starting_nothing),
            patch("vim_ai_follower.backends.nvim_connect.time.sleep"),
            patch(
                "vim_ai_follower.backends.nvim_connect.time.monotonic",
                side_effect=[0.0, 99.0],
            ),
            pytest.raises(nvim_connect.NvimNeverListened),
        ):
            nvim_connect.launch_standalone_nvim("@1")
        assert present_at_launch == [False], "the dead socket file was still there at launch"


def test_launch_standalone_nvim_never_removes_a_socket_someone_answers_on() -> None:
    with _short_socket_dir() as directory:
        sock_path = directory / "nvim-@1.sock"
        with fake_nvim(sock_path):
            launcher = MagicMock()
            with _patched_launcher(sock_path, launcher):
                assert nvim_connect.launch_standalone_nvim("@1") == str(sock_path)
            assert sock_path.is_socket()
            # Adopted, not relaunched: a second window's nvim would die on
            # "address already in use" and stay on screen as an orphan.
            launcher.assert_not_called()


def test_launch_standalone_nvim_reports_a_socket_path_it_cannot_clear() -> None:
    # A DIRECTORY at the socket path: nobody answers, and unlinking it raises
    # (EPERM on macOS). That OSError used to escape past every caller.
    with _short_socket_dir() as directory:
        sock_path = directory / "nvim-@1.sock"
        sock_path.mkdir()
        launcher = MagicMock()
        with (
            _patched_launcher(sock_path, launcher),
            pytest.raises(nvim_connect.SocketPathBlocked) as raised,
        ):
            nvim_connect.launch_standalone_nvim("@1")
        assert str(raised.value).startswith(f"cannot clear {sock_path}: ")
        assert isinstance(raised.value, nvim_connect.StandaloneLaunchFailed)
        launcher.assert_not_called()


def test_launch_standalone_nvim_attaches_once_the_socket_accepts(tmp_path: Path) -> None:
    sock_path = tmp_path / "nvim-@1.sock"
    with (
        _patched_launcher(sock_path, lambda *_a, **_k: None),
        patch(
            "vim_ai_follower.backends.nvim_connect.nvim_answers",
            # not up before the launch, then two polls before it answers
            side_effect=[False, False, False, True, True],
        ),
        patch("vim_ai_follower.backends.nvim_connect.time.sleep") as fake_sleep,
    ):
        assert nvim_connect.launch_standalone_nvim("@1") == str(sock_path)
    assert fake_sleep.call_count == 2


def test_launch_standalone_nvim_honors_a_shorter_wait(tmp_path: Path) -> None:
    sock_path = tmp_path / "nvim-@1.sock"  # never answers
    with (
        _patched_launcher(sock_path, lambda *_a, **_k: None),
        patch("vim_ai_follower.backends.nvim_connect.time.sleep"),
        patch(
            "vim_ai_follower.backends.nvim_connect.time.monotonic",
            side_effect=[0.0, 3.1],
        ),
        pytest.raises(nvim_connect.NvimNeverListened) as raised,
    ):
        nvim_connect.launch_standalone_nvim("@1", wait_seconds=3.0)
    assert str(raised.value).endswith("within 3 s")


def test_launch_standalone_nvim_detects_iterm_from_term_program(tmp_path: Path) -> None:
    sock_path = tmp_path / "nvim-@1.sock"
    with (
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=sock_path,
        ),
        patch("vim_ai_follower.backends.nvim_connect.subprocess.run") as run,
        patch(
            "vim_ai_follower.backends.nvim_connect.nvim_answers", side_effect=[False, True, True]
        ),
        patch("vim_ai_follower.backends.nvim_connect.shutil.which", return_value=None),
        patch("vim_ai_follower.backends.nvim_connect._vimr_app_present", return_value=False),
        patch.dict("os.environ", {"TERM_PROGRAM": "iTerm.app"}, clear=False),
    ):
        nvim_connect.launch_standalone_nvim("@1")
    script = run.call_args_list[0].args[0][2]
    assert 'application id "com.googlecode.iterm2"' in script


def test_launch_standalone_nvim_falls_back_to_terminal_when_not_iterm(tmp_path: Path) -> None:
    sock_path = tmp_path / "nvim-@1.sock"
    with (
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=sock_path,
        ),
        patch("vim_ai_follower.backends.nvim_connect.subprocess.run") as run,
        patch(
            "vim_ai_follower.backends.nvim_connect.nvim_answers", side_effect=[False, True, True]
        ),
        patch("vim_ai_follower.backends.nvim_connect.shutil.which", return_value=None),
        patch("vim_ai_follower.backends.nvim_connect._vimr_app_present", return_value=False),
        patch.dict("os.environ", {"TERM_PROGRAM": "Apple_Terminal"}, clear=False),
    ):
        nvim_connect.launch_standalone_nvim("@1")
    script = run.call_args_list[0].args[0][2]
    assert 'tell app "Terminal"' in script


def test_resolve_launched_gives_up_after_bounded_wait_when_socket_never_appears(
    tmp_path: Path,
) -> None:
    """If nvim never binds the socket (crashed, hung), resolve_nvim_target
    must still return rather than block forever — the bound is enforced via
    time.monotonic, not iteration count."""
    sock_path = tmp_path / "nvim-@1.sock"  # deliberately never created

    fake_now = [0.0]

    def _fake_monotonic() -> float:
        # advance the clock well past the timeout on every check so the
        # bounded loop exits after a couple of iterations instead of really
        # spinning for the full ~3s wall-clock wait.
        fake_now[0] += 10.0
        return fake_now[0]

    with (
        patch("vim_ai_follower.backends.nvim_connect.discover_adopt_socket", return_value=None),
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=sock_path,
        ),
        patch("vim_ai_follower.backends.nvim_connect.subprocess.run"),
        patch("vim_ai_follower.backends.nvim_connect.time.sleep"),
        patch("vim_ai_follower.backends.nvim_connect.time.monotonic", side_effect=_fake_monotonic),
    ):
        sock, launched = nvim_connect.resolve_nvim_target("%1", "@1", adopt=False)
    assert (sock, launched) == (str(sock_path), True)
    assert not sock_path.exists()


def test_vimr_app_present_reflects_the_applications_bundle() -> None:
    with patch("vim_ai_follower.backends.nvim_connect.Path.exists", return_value=True):
        assert nvim_connect._vimr_app_present() is True
    with patch("vim_ai_follower.backends.nvim_connect.Path.exists", return_value=False):
        assert nvim_connect._vimr_app_present() is False


class _RecordsSocketDir:
    """A subprocess.run stand-in that records whether the socket's directory
    existed at the moment nvim would have been told to `--listen` there."""

    def __init__(self, sock_path: Path) -> None:
        self.sock_path = sock_path
        self.seen: list[bool] = []

    def __call__(self, *_args: object, **_kwargs: object) -> None:
        self.seen.append(self.sock_path.parent.is_dir())


def test_launch_standalone_nvim_creates_the_socket_directory_first(tmp_path: Path) -> None:
    """On a fresh install nothing has created the cache dir yet when a
    standalone `start` runs (outside tmux no keybinding claim writes there
    first), and nvim refuses `--listen` into a missing directory — measured
    through the real CLI, 2026-09-29: "Failed to --listen: no such file or
    directory"."""
    sock_path = tmp_path / "fresh-cache" / "nvim-term-x.sock"
    run = _RecordsSocketDir(sock_path)
    with (
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=sock_path,
        ),
        patch("vim_ai_follower.backends.nvim_connect.subprocess.run", side_effect=run),
        patch("vim_ai_follower.backends.nvim_connect.time.sleep"),
        patch(
            "vim_ai_follower.backends.nvim_connect.shutil.which", return_value="/usr/bin/nvim-qt"
        ),
        patch("vim_ai_follower.backends.nvim_connect._vimr_app_present", return_value=False),
        patch("vim_ai_follower.backends.nvim_connect.time.monotonic", side_effect=[0.0, 0.0, 99.0]),
        pytest.raises(nvim_connect.NvimNeverListened),  # the recorder starts no nvim
    ):
        nvim_connect.launch_standalone_nvim("term-x")
    assert run.seen == [True]


def test_resolve_launched_creates_the_socket_directory_first(tmp_path: Path) -> None:
    """The tmux launch path's twin of the standalone case above."""
    sock_path = tmp_path / "fresh-cache" / "nvim-@1.sock"
    run = _RecordsSocketDir(sock_path)
    with (
        patch("vim_ai_follower.backends.nvim_connect.discover_adopt_socket", return_value=None),
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=sock_path,
        ),
        patch("vim_ai_follower.backends.nvim_connect.subprocess.run", side_effect=run),
        patch("vim_ai_follower.backends.nvim_connect.time.sleep"),
        patch("vim_ai_follower.backends.nvim_connect.time.monotonic", side_effect=[0.0, 0.0, 99.0]),
    ):
        nvim_connect.resolve_nvim_target("%1", "@1", adopt=False)
    assert run.seen == [True]


# --- bounded RPC: rpc_deadline, nvim_answers, sweep_stale_sockets ---------


def test_rpc_timeout_is_an_oserror_so_every_dead_socket_path_catches_it() -> None:
    assert issubclass(nvim_connect.RpcTimeout, OSError)
    assert nvim_connect.RPC_TIMEOUT_SECONDS == 10.0
    assert nvim_connect.CONNECT_TIMEOUT_SECONDS == 3.0
    assert nvim_connect.LIVENESS_TIMEOUT_SECONDS == 1.0


def test_rpc_deadline_raises_rpc_timeout_and_restores_the_previous_handler() -> None:
    def sentinel(_signum: int, _frame: object) -> None:
        return None

    previous = signal.signal(signal.SIGALRM, sentinel)
    try:
        started = time.monotonic()
        with (
            pytest.raises(nvim_connect.RpcTimeout, match=r"within 0\.05 s"),
            nvim_connect.rpc_deadline(0.05),
        ):
            time.sleep(2)
        assert time.monotonic() - started < 1.0
        assert signal.getsignal(signal.SIGALRM) is sentinel
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    finally:
        signal.signal(signal.SIGALRM, previous)


def test_rpc_deadline_that_is_met_leaves_no_timer_behind() -> None:
    with nvim_connect.rpc_deadline(5.0):
        pass
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_a_nested_rpc_deadline_keeps_the_shorter_outer_one() -> None:
    started = time.monotonic()
    with (
        pytest.raises(nvim_connect.RpcTimeout),
        nvim_connect.rpc_deadline(0.1),
        nvim_connect.rpc_deadline(5.0),
    ):
        time.sleep(2)
    assert time.monotonic() - started < 1.0
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_a_shorter_inner_rpc_deadline_fires_and_gives_the_outer_one_back() -> None:
    with nvim_connect.rpc_deadline(5.0):
        with pytest.raises(nvim_connect.RpcTimeout), nvim_connect.rpc_deadline(0.05):
            time.sleep(2)
        remaining, _interval = signal.getitimer(signal.ITIMER_REAL)
        assert 3.0 < remaining <= 5.0
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_an_outer_rpc_deadline_that_expired_inside_an_inner_one_still_fires() -> None:
    with pytest.raises(nvim_connect.RpcTimeout), nvim_connect.rpc_deadline(5.0):
        with (
            patch(
                "vim_ai_follower.backends.nvim_connect.time.monotonic",
                side_effect=[0.0, 99.0],
            ),
            nvim_connect.rpc_deadline(0.5),
        ):
            pass
        time.sleep(2)  # the outer deadline is long gone: it fires at once


def test_a_longer_inner_rpc_deadline_met_leaves_the_outer_one_running() -> None:
    with nvim_connect.rpc_deadline(5.0):
        with nvim_connect.rpc_deadline(10.0):
            pass
        remaining, _interval = signal.getitimer(signal.ITIMER_REAL)
        assert 3.0 < remaining <= 5.0
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_rpc_deadline_outside_the_main_thread_is_a_no_op() -> None:
    outcome: list[str] = []

    def worker() -> None:
        with nvim_connect.rpc_deadline(0.01):
            time.sleep(0.1)
        outcome.append("finished")

    handler_before = signal.getsignal(signal.SIGALRM)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(5)
    assert outcome == ["finished"]
    assert signal.getsignal(signal.SIGALRM) is handler_before


def test_nvim_answers_false_when_there_is_no_socket_file() -> None:
    with _short_socket_dir() as directory:
        assert nvim_connect.nvim_answers(str(directory / "nvim-@1.sock")) is False


def test_nvim_answers_false_for_a_dead_nvims_socket_file() -> None:
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        _dead_nvims_socket(path)
        assert nvim_connect.nvim_answers(str(path)) is False


def test_nvim_answers_false_for_a_listener_that_never_answers() -> None:
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        with fake_nvim(path, reply=None):
            started = time.monotonic()
            assert nvim_connect.nvim_answers(str(path), timeout=0.2) is False
            assert time.monotonic() - started < 1.0


@pytest.mark.parametrize(
    "garbage",
    [
        b"HTTP/1.1 400 Bad Request\r\n\r\n",
        b"\x94\x01\xcd\x03\xe7\xc0\x81\xa4mode\xa1n",  # [1, 999, nil, {...}]: not ours
        b"\x94\x01\x01\xa8an error\xc0",  # [1, 1, "an error", nil]: an error
        b"\x94",  # cut short
    ],
)
def test_nvim_answers_false_for_a_listener_that_answers_garbage(garbage: bytes) -> None:
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        with fake_nvim(path, reply=garbage):
            assert nvim_connect.nvim_answers(str(path), timeout=0.3) is False


def test_probe_calls_a_listener_that_hangs_up_garbled() -> None:
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        with fake_nvim(path, reply="hang up"):
            assert nvim_connect.probe_nvim(str(path), timeout=1.0) == "garbled"


class _ScriptedProbe:
    """A stand-in for the probe's socket: `connect`/`recv` raise or return
    what the test scripted (the kernel's rare answers, made deterministic)."""

    def __init__(self, connect_error: OSError | None, recv_error: OSError | None) -> None:
        self.connect_error = connect_error
        self.recv_error = recv_error

    def settimeout(self, _seconds: float) -> None:
        return None

    def connect(self, _path: str) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    def sendall(self, _data: bytes) -> None:
        return None

    def recv(self, _size: int) -> bytes:
        if self.recv_error is not None:
            raise self.recv_error
        return b"\x94"  # a byte at a time, never enough

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("connect_error", "recv_error", "verdict"),
    [
        # A full listen backlog: a frozen nvim stops accepting, so connect waits.
        (TimeoutError("timed out"), None, "silent"),
        (None, ConnectionResetError(54, "reset"), "garbled"),
    ],
)
def test_probe_verdicts_for_what_the_kernel_rarely_answers(
    connect_error: OSError | None, recv_error: OSError | None, verdict: str
) -> None:
    probe = _ScriptedProbe(connect_error, recv_error)
    with patch("vim_ai_follower.backends.nvim_connect.socket.socket", return_value=probe):
        assert nvim_connect.probe_nvim("/tmp/x.sock", timeout=1.0) == verdict


def test_probe_that_trickles_past_its_deadline_is_silent() -> None:
    # Bytes keep coming, too slowly: the probe's own deadline ends it, not
    # the per-recv socket timeout.
    probe = _ScriptedProbe(None, None)
    with (
        patch("vim_ai_follower.backends.nvim_connect.socket.socket", return_value=probe),
        patch(
            "vim_ai_follower.backends.nvim_connect.time.monotonic",
            side_effect=[0.0, 0.1, 0.2, 5.0],
        ),
    ):
        assert nvim_connect.probe_nvim("/tmp/x.sock", timeout=1.0) == "silent"


def test_nvim_answers_true_for_a_well_formed_get_mode_reply() -> None:
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        with fake_nvim(path):
            assert nvim_connect.nvim_answers(str(path)) is True


def test_nvim_answers_true_for_a_real_nvim(headless_nvim: str) -> None:
    assert nvim_connect.nvim_answers(headless_nvim) is True


def test_a_frozen_nvim_times_out_the_bounded_attach_and_its_requests(headless_nvim: str) -> None:
    """Measured first (Step 1 of the plan's Task 16): SIGALRM interrupts
    pynvim's asyncio loop both inside attach and inside a request, and the
    process connects again once the nvim is resumed.

    The hang guard resumes the nvim after _HANG_GUARD_SECONDS: with a
    deadline gone, the blocked call then returns and `pytest.raises` FAILS
    instead of hanging the run (CI has no pytest-timeout). It cannot be a
    SIGALRM, which is what the code under test uses."""
    connected = nvim_connect.bounded_attach(headless_nvim)
    pid = connected.call("getpid")
    os.kill(pid, signal.SIGSTOP)
    hang_guard = threading.Timer(_HANG_GUARD_SECONDS, os.kill, (pid, signal.SIGCONT))
    hang_guard.start()
    try:
        with patch("vim_ai_follower.backends.nvim_connect.CONNECT_TIMEOUT_SECONDS", 0.2):
            started = time.monotonic()
            with pytest.raises(nvim_connect.RpcTimeout):
                nvim_connect.bounded_attach(headless_nvim)
            assert time.monotonic() - started < 2.0
        with patch("vim_ai_follower.backends.nvim_connect.RPC_TIMEOUT_SECONDS", 0.2):
            started = time.monotonic()
            with pytest.raises(nvim_connect.RpcTimeout):
                connected.api.get_mode()
            assert time.monotonic() - started < 2.0
        assert nvim_connect.nvim_answers(headless_nvim, timeout=0.2) is False
    finally:
        hang_guard.cancel()
        os.kill(pid, signal.SIGCONT)
    assert nvim_connect.bounded_attach(headless_nvim).api.get_mode()["mode"] == "n"
    assert nvim_connect.nvim_answers(headless_nvim) is True


_SLOW_LUA = "vim.uv.sleep(600)"


def test_unbounded_requests_lift_the_deadline_on_that_connection_only(headless_nvim: str) -> None:
    # The typing of an animation waits for an nvim that is waiting on its
    # user; everything else on the connection keeps the deadline.
    connected = nvim_connect.bounded_attach(headless_nvim)
    with patch("vim_ai_follower.backends.nvim_connect.RPC_TIMEOUT_SECONDS", 0.2):
        with pytest.raises(nvim_connect.RpcTimeout):
            connected.exec_lua(_SLOW_LUA, [])
        # A fresh connection: the abandoned request's late answer would end
        # the next request on this one (see the review of T16).
        connected = nvim_connect.bounded_attach(headless_nvim)
        with nvim_connect.unbounded_requests(connected):
            with nvim_connect.unbounded_requests(connected):
                connected.exec_lua(_SLOW_LUA, [])
            connected.exec_lua(_SLOW_LUA, [])  # still lifted: nested, not reset
        with pytest.raises(nvim_connect.RpcTimeout):
            connected.exec_lua(_SLOW_LUA, [])


def test_unbounded_requests_is_a_no_op_on_a_connection_it_did_not_bound() -> None:
    nvim = MagicMock()
    with nvim_connect.unbounded_requests(nvim):
        nvim.api.get_mode()
    nvim.api.get_mode.assert_called_once_with()


def _age(path: Path, seconds: float) -> None:
    """Backdate a socket file's mtime (os.utime follows the path; a socket
    file takes it like any other)."""
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_sweep_stale_sockets_removes_only_unnamed_sockets_nobody_answers_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _short_socket_dir() as directory:
        monkeypatch.setattr(cache, "CACHE_DIR", directory)
        dead = directory / "nvim-@71.sock"
        _dead_nvims_socket(dead)
        named_by_its_window = directory / "nvim-@73.sock"
        _dead_nvims_socket(named_by_its_window)
        (directory / "@73.pane").write_text("{not json")
        named_as_a_target = directory / "nvim-@75.sock"
        _dead_nvims_socket(named_as_a_target)
        FollowerState.set("@74", "nvim", str(named_as_a_target), base_dir=directory)
        not_a_socket = directory / "nvim-@76.sock"
        not_a_socket.write_text("")
        booting = directory / "nvim-@77.sock"
        _dead_nvims_socket(booting)
        # A .pane gone between the listing and the read (a racing `stop`),
        # held still here as a dangling symlink: its window's path is kept.
        named_by_a_vanishing_pane = directory / "nvim-@78.sock"
        _dead_nvims_socket(named_by_a_vanishing_pane)
        (directory / "@78.pane").symlink_to(directory / "removed-meanwhile")
        with fake_nvim(directory / "nvim-@72.sock") as answering:
            for path in directory.glob("nvim-*.sock"):
                if path != booting:
                    _age(path, nvim_connect.SOCKET_SWEEP_GRACE_SECONDS + 5)
            assert nvim_connect.sweep_stale_sockets() == [str(dead)]
            assert answering.path.is_socket()
        assert not dead.exists()
        assert named_by_its_window.is_socket()
        assert named_as_a_target.is_socket()
        assert not_a_socket.exists()
        assert booting.is_socket(), "a launch still booting lost its socket"
        assert named_by_a_vanishing_pane.is_socket()


@pytest.mark.parametrize("reply", [None, b"HTTP/1.1 400 Bad Request\r\n\r\n"])
def test_sweep_stale_sockets_never_unlinks_a_socket_someone_listens_on(
    monkeypatch: pytest.MonkeyPatch, reply: bytes | None
) -> None:
    # Silent is an nvim busy in `:!make` (or stopped); garbled is a listener
    # that is not an nvim. Neither is "absent": the file stays under them.
    with _short_socket_dir() as directory:
        monkeypatch.setattr(cache, "CACHE_DIR", directory)
        listened_on = directory / "nvim-@79.sock"
        with fake_nvim(listened_on, reply=reply):
            _age(listened_on, nvim_connect.SOCKET_SWEEP_GRACE_SECONDS + 5)
            assert nvim_connect.sweep_stale_sockets() == []
            assert listened_on.is_socket()


def test_probe_nvim_remembers_its_last_verdict_per_socket() -> None:
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        assert nvim_connect.last_probe_verdict(str(path)) is None
        assert nvim_connect.probe_nvim(str(path)) == "absent"
        assert nvim_connect.last_probe_verdict(str(path)) == "absent"
        with fake_nvim(path, reply=None):
            assert nvim_connect.probe_nvim(str(path), timeout=0.2) == "silent"
            assert nvim_connect.last_probe_verdict(str(path)) == "silent"
        path.unlink()
        with fake_nvim(path):
            assert nvim_connect.probe_nvim(str(path)) == "answers"
            assert nvim_connect.last_probe_verdict(str(path)) == "answers"


def test_sweep_stale_sockets_with_no_cache_dir_removes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "missing")
    assert nvim_connect.sweep_stale_sockets() == []


def test_answering_socket_adopts_only_an_nvim(monkeypatch: pytest.MonkeyPatch) -> None:
    with _short_socket_dir() as directory:
        monkeypatch.setattr(cache, "CACHE_DIR", directory)
        path = directory / "nvim-@1.sock"
        with fake_nvim(path, reply=None):
            assert nvim_connect.answering_socket("@1") is None
        path.unlink()
        with fake_nvim(path):
            assert nvim_connect.answering_socket("@1") == str(path)


def test_launch_standalone_nvim_neither_adopts_nor_removes_a_listener_that_is_not_nvim() -> None:
    """Slot/launch residual "adopt-if-answering trusts whatever answers": a
    connection accepted is not an nvim. It is not adopted (a follower would
    be persisted on it) and not unlinked (someone is listening there)."""
    with _short_socket_dir() as directory:
        sock_path = directory / "nvim-@1.sock"
        with fake_nvim(sock_path, reply=None):
            launcher = MagicMock()
            with (
                _patched_launcher(sock_path, launcher),
                patch("vim_ai_follower.backends.nvim_connect.LIVENESS_TIMEOUT_SECONDS", 0.2),
                pytest.raises(nvim_connect.SocketPathBlocked) as raised,
            ):
                nvim_connect.launch_standalone_nvim("@1")
            assert sock_path.is_socket()
            launcher.assert_not_called()
        assert "not an nvim" in str(raised.value)
