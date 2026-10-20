from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
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


_SPLIT_PANE = "%7"


def _split_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    """`tmux split-window -P -F #{pane_id}` answering with the new pane."""
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=f"{_SPLIT_PANE}\n")


def _patched_split(sock_path: Path, run: Any = _split_run) -> AbstractContextManager[Any]:
    stack = ExitStack()
    stack.enter_context(
        patch("vim_ai_follower.backends.nvim_connect.discover_adopt_socket", return_value=None)
    )
    stack.enter_context(
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=sock_path,
        )
    )
    stack.enter_context(patch("vim_ai_follower.backends.nvim_connect.subprocess.run", run))
    # The scan for a live `--listen` process runs `ps` through the same
    # subprocess.run this helper stands in for: nobody listens here.
    stack.enter_context(
        patch(
            "vim_ai_follower.backends.nvim_connect.refused_by_a_live_nvim",
            return_value=False,
        )
    )
    return stack


def test_resolve_launches_when_adopt_requested_but_nothing_found(tmp_path: Path) -> None:
    sock_path = tmp_path / "nvim-@1.sock"
    run = MagicMock(side_effect=_split_run)
    with (
        _patched_split(sock_path, run),
        # nobody on the socket before the split, then the split's nvim answers
        patch("vim_ai_follower.backends.nvim_connect.nvim_ready", side_effect=[False, True]),
    ):
        sock, launched = nvim_connect.resolve_nvim_target("%1", "@1", adopt=True)
    assert (sock, launched) == (str(sock_path), True)
    # adopt requested but nothing found -> still splits a pane running nvim --listen
    args = run.call_args_list[0].args[0]
    assert args[:3] == ["tmux", "split-window", "-h"]
    assert args[-3:] == ["nvim", "--listen", str(sock_path)]


def test_resolve_adopts_when_socket_found() -> None:
    with (
        patch(
            "vim_ai_follower.backends.nvim_connect.discover_adopt_socket", return_value="/s.sock"
        ),
    ):
        assert nvim_connect.resolve_nvim_target("%1", "@1", adopt=True) == ("/s.sock", False)


def test_resolve_launches_when_no_adopt(tmp_path: Path) -> None:
    sock_path = tmp_path / "nvim-@1.sock"
    run = MagicMock(side_effect=_split_run)
    with (
        _patched_split(sock_path, run),
        patch("vim_ai_follower.backends.nvim_connect.nvim_ready", side_effect=[False, True]),
    ):
        sock, launched = nvim_connect.resolve_nvim_target("%1", "@1", adopt=False)
    assert (sock, launched) == (str(sock_path), True)
    # split a pane running `nvim --listen <sock>`, asking tmux for its pane id
    args = run.call_args_list[0].args[0]
    assert args == [
        "tmux",
        "split-window",
        "-h",
        "-P",
        "-F",
        "#{pane_id}",
        "-t",
        "%1",
        "nvim",
        "--listen",
        str(sock_path),
    ]


def test_resolve_launched_waits_for_an_rpc_answer_not_the_socket_file(tmp_path: Path) -> None:
    """Readiness is an nvim ANSWERING on the socket, not the socket file: an
    nvim spinning in its init (or one stopped, or not an nvim) binds the file
    and never answers, and the old existence check reported it attached."""
    sock_path = tmp_path / "nvim-@1.sock"
    with (
        _patched_split(sock_path),
        # before the split, then two polls before the split's nvim answers
        patch(
            "vim_ai_follower.backends.nvim_connect.nvim_ready",
            side_effect=[False, False, False, True],
        ) as answers,
        patch("vim_ai_follower.backends.nvim_connect.Path.exists", return_value=True),
        patch("vim_ai_follower.backends.nvim_connect._remove_dead_socket"),
        patch("vim_ai_follower.backends.nvim_connect.time.sleep") as fake_sleep,
    ):
        sock, launched = nvim_connect.resolve_nvim_target("%1", "@1", adopt=False)
    assert (sock, launched) == (str(sock_path), True)
    assert answers.call_count == 4
    assert fake_sleep.call_count == 2  # it waited for the answer, though the file "existed"


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


def test_launch_standalone_nvim_raises_when_no_nvim_ever_listens() -> None:
    """A launcher that exits 0 and starts nothing (an nvim-qt that cannot find
    nvim, a Terminal window whose nvim dies on a config error) used to hand
    back the socket anyway, and `start` said "attached" to nothing. Past the
    deadline it is an error, naming the launcher and the socket. The deadline
    is a GUI app's cold launch, longer than a tmux split's. A short socket
    directory: under pytest's tmp_path the path passes the AF_UNIX limit, the
    connect fails like a refusal and runs the process scan, which reads the
    clock too."""
    with _short_socket_dir() as directory:
        _assert_no_nvim_ever_listens(directory / "nvim-@1.sock")  # never created


def _assert_no_nvim_ever_listens(sock_path: Path) -> None:
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
    # The scan for a live `--listen` process runs `ps` through the same
    # subprocess.run this helper stands in for: nobody listens here.
    stack.enter_context(
        patch(
            "vim_ai_follower.backends.nvim_connect.refused_by_a_live_nvim",
            return_value=False,
        )
    )
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


def test_a_split_nvim_that_never_answers_is_an_error_and_its_pane_stays(
    tmp_path: Path,
) -> None:
    """An nvim that never answers (spinning in a broken init, crashed, hung)
    used to be returned as the follower after 3 s, and `start` said
    "attached" to nothing. Past the deadline it is an error naming the socket
    and the wait; the pane is left open so the user can read nvim's own
    error (Alberto's answer B, 2026-10-16), never killed."""
    sock_path = tmp_path / "nvim-@1.sock"
    run = MagicMock(side_effect=_split_run)
    with (
        _patched_split(sock_path, run),
        patch("vim_ai_follower.backends.nvim_connect.nvim_ready", return_value=False),
        patch("vim_ai_follower.backends.nvim_connect.time.sleep") as fake_sleep,
        patch(
            "vim_ai_follower.backends.nvim_connect.time.monotonic",
            # the deadline; then per check: time left, and the pause after it
            side_effect=[0.0, 0.0, 0.0, 2.9, 2.9, 3.1],
        ),
        patch(
            "vim_ai_follower.backends.nvim_connect.TmuxPane.running_command",
            return_value="nvim",
        ),
        pytest.raises(nvim_connect.SplitNvimNeverAnswered) as raised,
    ):
        nvim_connect.resolve_nvim_target("%1", "@1", adopt=False)
    assert str(raised.value) == (
        f"nvim did not answer on {sock_path} within 3 s; its pane is left open so you can read why"
    )
    assert raised.value.sock == str(sock_path)
    assert raised.value.pane_id == _SPLIT_PANE
    assert fake_sleep.call_count == 2  # still waiting at 2.9 s, gave up at 3 s
    assert [call.args[0][:2] for call in run.call_args_list] == [["tmux", "split-window"]]


def test_the_hook_waits_three_seconds_and_start_may_wait_longer(tmp_path: Path) -> None:
    assert nvim_connect.START_SPLIT_WAIT_SECONDS == 10.0
    sock_path = tmp_path / "nvim-@1.sock"
    with (
        _patched_split(sock_path),
        patch("vim_ai_follower.backends.nvim_connect.nvim_ready", return_value=False),
        patch("vim_ai_follower.backends.nvim_connect.time.sleep") as fake_sleep,
        patch(
            "vim_ai_follower.backends.nvim_connect.time.monotonic",
            side_effect=[0.0, 3.1, 3.1, 9.9, 9.9, 10.1],
        ),
        patch(
            "vim_ai_follower.backends.nvim_connect.TmuxPane.running_command",
            return_value="nvim",
        ),
        pytest.raises(nvim_connect.SplitNvimNeverAnswered) as raised,
    ):
        nvim_connect.resolve_nvim_target(
            "%1", "@1", adopt=False, wait_seconds=nvim_connect.START_SPLIT_WAIT_SECONDS
        )
    assert "within 10 s" in str(raised.value)
    assert fake_sleep.call_count == 2


def test_a_split_whose_nvim_exited_says_its_pane_closed(tmp_path: Path) -> None:
    # nvim that quits at startup (`cquit` in an init, a crash) takes its pane
    # with it: "left open" would send the user looking for a pane that is gone.
    sock_path = tmp_path / "nvim-@1.sock"
    with (
        _patched_split(sock_path),
        patch("vim_ai_follower.backends.nvim_connect.nvim_ready", return_value=False),
        patch("vim_ai_follower.backends.nvim_connect.time.sleep"),
        patch("vim_ai_follower.backends.nvim_connect.time.monotonic", side_effect=[0.0, 3.1]),
        patch(
            "vim_ai_follower.backends.nvim_connect.TmuxPane.running_command",
            return_value=None,
        ),
        pytest.raises(nvim_connect.SplitNvimNeverAnswered) as raised,
    ):
        nvim_connect.resolve_nvim_target("%1", "@1", adopt=False)
    assert str(raised.value) == (
        f"nvim did not answer on {sock_path} within 3 s; its pane has closed (nvim exited)"
    )


def test_a_split_that_answered_after_its_caller_gave_up_is_attached_not_split_again() -> None:
    # The pane of a slow-booting nvim is left open by the failure above; once
    # it answers, the next attempt attaches to it. A second split's nvim would
    # die on "address already in use" next to it.
    with _short_socket_dir() as directory:
        sock_path = directory / "nvim-@1.sock"
        run = MagicMock(side_effect=_split_run)
        with (
            fake_nvim(sock_path),
            _patched_split(sock_path, run),
            patch("vim_ai_follower.backends.nvim_connect._did_enter", return_value=True),
        ):
            assert nvim_connect.resolve_nvim_target("%1", "@1", adopt=False) == (
                str(sock_path),
                True,
            )
        run.assert_not_called()


def test_a_split_clears_a_dead_nvims_socket_file_first() -> None:
    # A SIGKILLed follower nvim leaves its socket file: the new split's nvim
    # could not bind the path.
    with _short_socket_dir() as directory:
        sock_path = directory / "nvim-@1.sock"
        _dead_nvims_socket(sock_path)
        present_at_split: list[bool] = []

        def split_recording(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            present_at_split.append(sock_path.exists())
            return _split_run(*args, **kwargs)

        with (
            _patched_split(sock_path, split_recording),
            patch("vim_ai_follower.backends.nvim_connect.time.sleep"),
            patch(
                "vim_ai_follower.backends.nvim_connect.time.monotonic",
                # the pre-split probe's budget, the deadline, then past it
                side_effect=[0.0, 0.0, 9.0],
            ),
            patch(
                "vim_ai_follower.backends.nvim_connect.TmuxPane.running_command",
                return_value="nvim",
            ),
            pytest.raises(nvim_connect.SplitNvimNeverAnswered),
        ):
            nvim_connect.resolve_nvim_target("%1", "@1", adopt=False)
        assert present_at_split == [False]


def test_a_split_never_lands_on_a_socket_someone_else_listens_on() -> None:
    # The pane left open by a failed split still holds the socket while its
    # nvim spins: no second split over it, the same refusal as standalone.
    with _short_socket_dir() as directory:
        sock_path = directory / "nvim-@1.sock"
        run = MagicMock(side_effect=_split_run)
        with (
            fake_nvim(sock_path, reply=None),
            _patched_split(sock_path, run),
            pytest.raises(nvim_connect.SocketPathBlocked),
        ):
            nvim_connect.resolve_nvim_target("%1", "@1", adopt=False)
        run.assert_not_called()


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
        patch(
            "vim_ai_follower.backends.nvim_connect.refused_by_a_live_nvim",
            return_value=False,
        ),
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
    recorder = _RecordsSocketDir(sock_path)

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorder(*args, **kwargs)
        return _split_run()

    with (
        _patched_split(sock_path, run),
        patch("vim_ai_follower.backends.nvim_connect.nvim_ready", side_effect=[False, True]),
    ):
        nvim_connect.resolve_nvim_target("%1", "@1", adopt=False)
    assert recorder.seen == [True]


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


@pytest.mark.integration
def test_nvim_answers_true_for_a_real_nvim(headless_nvim: str) -> None:
    assert nvim_connect.nvim_answers(headless_nvim) is True


@pytest.mark.integration
def test_nvim_ready_true_for_a_started_real_nvim(headless_nvim: str) -> None:
    assert nvim_connect.nvim_ready(headless_nvim) is True


@pytest.mark.parametrize(
    ("did_enter_reply", "ready"),
    [
        (b"\x94\x01\x01\xc0\x01", True),  # v:vim_did_enter is 1
        (b"\x94\x01\x01\xc0\x00", False),  # still starting: its init has not run
        (b"\x94\x01\x01", False),  # a reply that stops short
        ("hang up", False),
        (None, False),  # held: a startup hit-enter prompt, or an init that spins
    ],
)
def test_nvim_ready_needs_an_nvim_that_has_finished_starting(
    did_enter_reply: Any, ready: bool
) -> None:
    # A TUI nvim answers get_mode for a few ms before its init runs (measured
    # 2026-10-17, one split in eight): an answer alone is no readiness.
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        with (
            fake_nvim(path, reply=did_enter_reply),
            patch("vim_ai_follower.backends.nvim_connect.nvim_answers", return_value=True),
        ):
            assert nvim_connect.nvim_ready(str(path), timeout=0.3) is ready


def test_nvim_ready_asks_nothing_more_of_a_socket_nobody_answers_on() -> None:
    with (
        patch("vim_ai_follower.backends.nvim_connect.nvim_answers", return_value=False),
        patch("vim_ai_follower.backends.nvim_connect._did_enter") as did_enter,
    ):
        assert nvim_connect.nvim_ready("/nowhere.sock") is False
    did_enter.assert_not_called()


@pytest.mark.parametrize(
    ("reply", "open_window"),
    [
        (b"\x94\x01\x01\xc0\xa0", False),  # getcmdwintype() is ""
        (b"\x94\x01\x01\xc0\xa1:", True),  # `q:`
        (b"\x94\x01\x01\xc0\xa1/", True),  # `q/`
        (b"\x94\x01\x01\xc1\xa0", None),  # an error answer, not a result
        (b"\x94\x01\x01\xc0\x01", None),  # a result that is no string
        (b"\x94\x01\x01\xc0", None),  # a reply that stops short
        ("hang up", None),
        ("hang up after the request", None),  # the read comes back empty
        (None, None),  # held: busy, or at a prompt after all
    ],
)
def test_in_command_line_window_reads_getcmdwintype(reply: Any, open_window: bool | None) -> None:
    # Inside `q:` get_mode reads "n", not blocking, and every window change
    # raises E11 (T22 re-review m2): only getcmdwintype() tells.
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        with fake_nvim(path, reply=reply):
            assert nvim_connect.in_command_line_window(str(path), timeout=0.3) is open_window


def test_in_command_line_window_is_none_for_a_socket_nobody_listens_on() -> None:
    with _short_socket_dir() as directory:
        assert nvim_connect.in_command_line_window(str(directory / "nowhere.sock")) is None


@pytest.mark.integration
def test_in_command_line_window_asks_a_real_nvim(headless_nvim: str) -> None:
    assert nvim_connect.in_command_line_window(headless_nvim) is False


def test_ready_split_socket_names_the_windows_launch_socket_only_when_ready() -> None:
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        with patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path", return_value=path
        ):
            assert nvim_connect.ready_split_socket("@1") is None
            with fake_nvim(path, reply=b"\x94\x01\x01\xc0\x01"):
                assert nvim_connect.ready_split_socket("@1") is None  # not a get_mode answer
                with patch("vim_ai_follower.backends.nvim_connect.nvim_answers", return_value=True):
                    assert nvim_connect.ready_split_socket("@1") == str(path)


@pytest.mark.integration
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


@pytest.mark.integration
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


def test_unbounded_requests_puts_the_deadline_back_after_an_exception_inside() -> None:
    # An animation that raises (a goto that fails mid-resume, a dead socket)
    # must not leave the connection unbounded: the next request on it — a
    # navigation, the cue — would then wait on a silent nvim for good.
    from types import SimpleNamespace

    def slow_request(*_args: Any, **_kwargs: Any) -> str:
        time.sleep(1.0)
        return "late"

    attached = SimpleNamespace(_session=SimpleNamespace(request=slow_request))
    with (
        patch("pynvim.attach", return_value=attached),
        patch("vim_ai_follower.backends.nvim_connect.RPC_TIMEOUT_SECONDS", 0.1),
    ):
        connected = nvim_connect.bounded_attach("/unused.sock")
        with (
            pytest.raises(ValueError, match="mid-animation"),
            nvim_connect.unbounded_requests(connected),
        ):
            raise ValueError("mid-animation")
        started = time.monotonic()
        with pytest.raises(nvim_connect.RpcTimeout):
            connected._session.request("nvim_get_mode")
    assert time.monotonic() - started < 0.8


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


@pytest.mark.parametrize(
    ("reply", "blocking"),
    [
        ("mode", False),
        (bytes([0x94, 0x01, 0x01, 0xC0, 0x82]) + b"\xa4mode\xa1r\xa8blocking\xc3", True),
        # The dictionary cut off before its `blocking` value: an answer all
        # the same, from an nvim not known to be blocking.
        (bytes([0x94, 0x01, 0x01, 0xC0, 0x82]) + b"\xa4mode\xa1r\xa8blocking", False),
    ],
    ids=["idle", "at-a-prompt", "cut-short"],
)
def test_probe_nvim_remembers_whether_the_nvim_is_waiting_on_its_user(
    reply: Any, blocking: bool
) -> None:
    # A hit-enter prompt answers get_mode `{"mode": "r", "blocking": true}`
    # and holds every other request: the hooks skip an ADOPTED one at once.
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        assert nvim_connect.last_probe_blocking(str(path)) is False
        with fake_nvim(path, reply=reply):
            assert nvim_connect.probe_nvim(str(path), timeout=0.3) == "answers"
        assert nvim_connect.last_probe_blocking(str(path)) is blocking


@pytest.mark.parametrize(
    ("reply", "mode"),
    [
        ("mode", "n"),
        (bytes([0x94, 0x01, 0x01, 0xC0, 0x82]) + b"\xa4mode\xa1r\xa8blocking\xc3", "r"),
        (bytes([0x94, 0x01, 0x01, 0xC0, 0x82]) + b"\xa4mode\xa2rm\xa8blocking\xc3", "rm"),
        (bytes([0x94, 0x01, 0x01, 0xC0, 0x82]) + b"\xa8blocking\xc2\xa4mode\xa2r?", "r?"),
        # The dictionary cut off inside the mode's string: no mode known.
        (bytes([0x94, 0x01, 0x01, 0xC0, 0x82]) + b"\xa4mode\xa2r", None),
        # Not a fixstr where the mode should be.
        (bytes([0x94, 0x01, 0x01, 0xC0, 0x82]) + b"\xa4mode\xc0\xa8blocking\xc3", None),
        # A reply with no mode at all, and one cut off right after the key:
        # the probe waits for the rest until the deadline, then knows none.
        (bytes([0x94, 0x01, 0x01, 0xC0, 0x81]) + b"\xa8blocking\xc3", None),
        (bytes([0x94, 0x01, 0x01, 0xC0, 0x82]) + b"\xa8blocking\xc3\xa4mode", None),
    ],
    ids=[
        "idle",
        "hit-enter",
        "more",
        "confirm-blocking-first",
        "cut-short",
        "not-a-string",
        "no-mode-key",
        "cut-after-the-key",
    ],
)
def test_probe_nvim_remembers_the_mode_it_answered(reply: Any, mode: str | None) -> None:
    # Which prompt holds a blocking nvim: a hit-enter ("r") or more-prompt
    # ("rm") is one the follower answers when it acts; anything else is not.
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        assert nvim_connect.last_probe_mode(str(path)) is None
        with fake_nvim(path, reply=reply):
            assert nvim_connect.probe_nvim(str(path), timeout=0.3) == "answers"
        assert nvim_connect.last_probe_mode(str(path)) == mode


@pytest.mark.parametrize(("rest", "mode"), [(b"\xa4mode\xa2rm", "rm"), (b"", None)])
def test_probe_nvim_reads_a_mode_that_arrives_in_a_later_piece(
    rest: bytes, mode: str | None
) -> None:
    # A reply split after `blocking`: the probe keeps reading until the mode
    # has arrived too, or until the nvim hangs up without one.
    head = bytes([0x94, 0x01, 0x01, 0xC0, 0x82]) + b"\xa8blocking\xc3"
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen()

        def serve() -> None:
            client, _address = server.accept()
            with client:
                client.recv(4096)
                client.sendall(head)
                time.sleep(0.05)
                if rest:
                    client.sendall(rest)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            assert nvim_connect.probe_nvim(str(path), timeout=2.0) == "answers"
        finally:
            thread.join(timeout=5)
            server.close()
        assert nvim_connect.last_probe_blocking(str(path)) is True
        assert nvim_connect.last_probe_mode(str(path)) == mode


def test_probe_nvim_stops_waiting_for_the_mode_once_its_deadline_is_spent() -> None:
    # `blocking` has arrived, the mode has not, and the nvim keeps the
    # connection open: the probe answers with what it has when its deadline
    # runs out between two pieces, instead of reading on (patched clock: the
    # second piece's wait starts past the deadline).
    head = bytes([0x94, 0x01, 0x01, 0xC0, 0x82]) + b"\xa8blocking\xc3"
    readings = iter([0.0, 0.0])
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen()
        probed = threading.Event()

        def serve() -> None:
            client, _address = server.accept()
            with client:
                client.recv(4096)
                client.sendall(head)
                probed.wait(5)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            with patch(
                "vim_ai_follower.backends.nvim_connect.time.monotonic",
                side_effect=lambda: next(readings, 1000.0),
            ):
                assert nvim_connect.probe_nvim(str(path), timeout=2.0) == "answers"
        finally:
            probed.set()
            thread.join(timeout=5)
            server.close()
        assert nvim_connect.last_probe_blocking(str(path)) is True
        assert nvim_connect.last_probe_mode(str(path)) is None


def test_probe_nvim_forgets_the_mode_of_an_nvim_that_stopped_answering() -> None:
    with _short_socket_dir() as directory:
        path = directory / "nvim-@1.sock"
        with fake_nvim(path):
            nvim_connect.probe_nvim(str(path))
        assert nvim_connect.last_probe_mode(str(path)) == "n"
        path.unlink(missing_ok=True)
        assert nvim_connect.probe_nvim(str(path)) == "absent"
        assert nvim_connect.last_probe_mode(str(path)) is None


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
                pytest.raises(nvim_connect.SocketPathBlocked) as raised,
            ):
                nvim_connect.launch_standalone_nvim("@1")
            assert sock_path.is_socket()
            launcher.assert_not_called()
        assert "not an nvim" in str(raised.value)


# --- a refused connect on a live nvim's socket (T19 fix round 1, I1) ------


def _listing(*command_lines: str) -> subprocess.CompletedProcess[str]:
    """`ps -A -ww -o command=` answering with these command lines."""
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout="".join(f"{line}\n" for line in command_lines)
    )


@pytest.mark.parametrize(
    "command_line",
    ["nvim --listen {sock}", "/opt/homebrew/bin/nvim --embed --listen {sock}"],
    ids=["tui", "embed-server"],
)
def test_a_refused_socket_a_live_nvim_listens_on_is_silent_not_absent(command_line: str) -> None:
    """macOS keeps a connection its client closed in the listen queue: after
    32 probes of an nvim that never accepts (spinning in its init, SIGSTOPped)
    connect is REFUSED, just as on a dead nvim's file. Errno cannot tell them
    apart; a live process launched with `--listen <sock>` can (measured
    2026-10-17: the TUI and its `--embed` server both carry it)."""
    with _short_socket_dir() as directory:
        sock = directory / "nvim-@1.sock"
        _dead_nvims_socket(sock)  # refused, like a full queue
        listing = _listing("zsh", command_line.format(sock=sock))
        with patch(
            "vim_ai_follower.backends.nvim_connect.subprocess.run", return_value=listing
        ) as run:
            assert nvim_connect.probe_nvim(str(sock)) == "silent"
            assert nvim_connect.is_listened_on_by_a_live_process(str(sock))
        assert run.call_args.args[0] == ["ps", "-A", "-ww", "-o", "command="]
        assert run.call_args.kwargs["timeout"] == nvim_connect.PROCESS_SCAN_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "command_line",
    [
        "nvim --listen {sock}.old",
        "nvim --listen {sock}x --embed",
        "nvim {sock}",  # editing the path, not listening on it
        "vim --listen-on {sock}",
    ],
)
def test_a_refused_socket_no_live_process_listens_on_is_absent(command_line: str) -> None:
    with _short_socket_dir() as directory:
        sock = directory / "nvim-@1.sock"
        _dead_nvims_socket(sock)
        listing = _listing(command_line.format(sock=sock))
        with patch("vim_ai_follower.backends.nvim_connect.subprocess.run", return_value=listing):
            assert nvim_connect.probe_nvim(str(sock)) == "absent"


def test_a_socket_path_with_a_space_still_matches_whole() -> None:
    with _short_socket_dir() as directory:
        sock = directory / "a b" / "nvim-@1.sock"
        sock.parent.mkdir()
        _dead_nvims_socket(sock)
        with patch(
            "vim_ai_follower.backends.nvim_connect.subprocess.run",
            return_value=_listing(f"nvim --embed --listen {sock}"),
        ):
            assert nvim_connect.probe_nvim(str(sock)) == "silent"
        with patch(
            "vim_ai_follower.backends.nvim_connect.subprocess.run",
            return_value=_listing(f"nvim --listen {directory}/a"),
        ):
            assert nvim_connect.probe_nvim(str(sock)) == "absent"


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess(args=[], returncode=1, stdout=""),
        subprocess.TimeoutExpired(cmd="ps", timeout=2.0),
        FileNotFoundError("ps"),
    ],
    ids=["ps-failed", "ps-timed-out", "no-ps"],
)
def test_a_process_scan_that_fails_keeps_the_socket_silent(outcome: object) -> None:
    # Fail closed: an unknown listener is a live one, never unlinked.
    with _short_socket_dir() as directory:
        sock = directory / "nvim-@1.sock"
        _dead_nvims_socket(sock)
        run = MagicMock()
        if isinstance(outcome, BaseException):
            run.side_effect = outcome
        else:
            run.return_value = outcome
        with patch("vim_ai_follower.backends.nvim_connect.subprocess.run", run):
            assert nvim_connect.probe_nvim(str(sock)) == "silent"


def test_a_missing_socket_file_is_absent_without_a_process_scan() -> None:
    with _short_socket_dir() as directory:
        with patch("vim_ai_follower.backends.nvim_connect.subprocess.run") as run:
            assert nvim_connect.probe_nvim(str(directory / "nvim-@1.sock")) == "absent"
        run.assert_not_called()


def test_a_refused_socket_a_live_nvim_listens_on_is_never_cleared_or_swept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _short_socket_dir() as directory:
        monkeypatch.setattr(cache, "CACHE_DIR", directory)
        sock = directory / "nvim-@1.sock"
        _dead_nvims_socket(sock)
        _age(sock, nvim_connect.SOCKET_SWEEP_GRACE_SECONDS + 5)
        with patch(
            "vim_ai_follower.backends.nvim_connect.subprocess.run",
            return_value=_listing(f"nvim --listen {sock}"),
        ):
            with pytest.raises(nvim_connect.SocketPathBlocked):
                nvim_connect._remove_dead_socket(str(sock))
            assert nvim_connect.sweep_stale_sockets() == []
        assert sock.is_socket()


# --- fix round 1, m1 / m3 -------------------------------------------------


def test_a_split_nvim_that_answers_but_has_not_started_is_named_so(tmp_path: Path) -> None:
    """At its startup hit-enter prompt the left-open split answers get_mode
    but never says vim_did_enter: it is an nvim, not "a listener that did not
    answer as an nvim", and the user is told what to do."""
    sock_path = tmp_path / "nvim-@1.sock"
    run = MagicMock(side_effect=_split_run)

    def answers_but_not_started(sock: str, timeout: float = 1.0) -> bool:
        nvim_connect._last_verdicts[sock] = "answers"
        return False

    with (
        _patched_split(sock_path, run),
        patch(
            "vim_ai_follower.backends.nvim_connect.nvim_ready",
            side_effect=answers_but_not_started,
        ),
        pytest.raises(nvim_connect.SplitNvimNotStarted) as raised,
    ):
        nvim_connect.resolve_nvim_target("%1", "@1", adopt=False)
    assert str(raised.value) == (
        f"nvim on {sock_path} has not finished starting (a prompt in its pane?); "
        "press Enter there or close the pane"
    )
    assert isinstance(raised.value, nvim_connect.StandaloneLaunchFailed)
    run.assert_not_called()


def test_nvim_ready_spends_one_budget_on_both_questions() -> None:
    with (
        patch("vim_ai_follower.backends.nvim_connect.nvim_answers", return_value=True) as answers,
        patch("vim_ai_follower.backends.nvim_connect._did_enter", return_value=True) as did_enter,
        patch("vim_ai_follower.backends.nvim_connect.time.monotonic", side_effect=[10.0, 10.75]),
    ):
        assert nvim_connect.nvim_ready("/s.sock", timeout=1.0)
    assert answers.call_args.args == ("/s.sock", 1.0)
    assert did_enter.call_args.args == ("/s.sock", pytest.approx(0.25))
    with (
        patch("vim_ai_follower.backends.nvim_connect.nvim_answers", return_value=True),
        patch("vim_ai_follower.backends.nvim_connect._did_enter") as did_enter,
        patch("vim_ai_follower.backends.nvim_connect.time.monotonic", side_effect=[10.0, 11.0]),
    ):
        assert not nvim_connect.nvim_ready("/s.sock", timeout=1.0)
    did_enter.assert_not_called()


def test_the_split_wait_never_runs_past_the_seconds_it_reports(tmp_path: Path) -> None:
    """Each readiness check and each pause is capped to the time left, so
    "within 3 s" is what the hook spent (review m3: 3.27-3.34 s before)."""
    sock_path = tmp_path / "nvim-@1.sock"
    with (
        _patched_split(sock_path),
        patch("vim_ai_follower.backends.nvim_connect.nvim_ready", return_value=False) as ready,
        patch("vim_ai_follower.backends.nvim_connect.time.sleep") as fake_sleep,
        patch(
            "vim_ai_follower.backends.nvim_connect.time.monotonic",
            # the deadline; then per check: time left, and the pause after it
            side_effect=[0.0, 0.0, 0.0, 2.6, 2.98, 3.0],
        ),
        patch(
            "vim_ai_follower.backends.nvim_connect.TmuxPane.running_command",
            return_value="nvim",
        ),
        patch("vim_ai_follower.backends.nvim_connect._remove_dead_socket"),
        pytest.raises(nvim_connect.SplitNvimNeverAnswered),
    ):
        nvim_connect.resolve_nvim_target("%1", "@1", adopt=False)
    assert [call.args for call in ready.call_args_list] == [
        (str(sock_path),),  # before the split
        (str(sock_path), 1.0),
        (str(sock_path), pytest.approx(0.4)),
    ]
    assert [call.args[0] for call in fake_sleep.call_args_list] == [
        nvim_connect._SOCKET_POLL_INTERVAL_SECONDS,
        pytest.approx(0.02),
    ]


# --- the same, on an adopted nvim's default socket (T19 fix round 2) ------


def _no_listening_process() -> AbstractContextManager[Any]:
    """`ps` lists nobody launched with `--listen`: an adopted nvim's own
    socket never carries one, so only its name can prove it alive."""
    return patch(
        "vim_ai_follower.backends.nvim_connect.subprocess.run", return_value=_listing("zsh")
    )


def _gone_pid() -> int:
    finished = subprocess.Popen(["true"])
    finished.wait()
    return finished.pid


def test_a_refused_default_socket_whose_nvim_lives_is_silent() -> None:
    """nvim names its default server `…/nvim.<user>/<random>/nvim.<pid>.<n>`
    (measured 2026-10-17 on 0.12.6: the pid is the nvim's own), and a
    SIGSTOPped adopted nvim refuses connects after 32 probes like any other.
    Its pid alive → silent, so another window's `stop` or hook never reads a
    live follower as gone."""
    with _short_socket_dir() as directory:
        sock = directory / f"nvim.{os.getpid()}.0"
        _dead_nvims_socket(sock)
        with _no_listening_process():
            assert nvim_connect.probe_nvim(str(sock)) == "silent"


def test_a_refused_default_socket_whose_pid_is_gone_is_absent() -> None:
    with _short_socket_dir() as directory:
        sock = directory / f"nvim.{_gone_pid()}.0"
        _dead_nvims_socket(sock)
        with _no_listening_process():
            assert nvim_connect.probe_nvim(str(sock)) == "absent"


def test_a_default_socket_whose_pid_we_may_not_signal_is_silent() -> None:
    # EPERM: the process exists (another user's): alive, not gone.
    with _short_socket_dir() as directory:
        sock = directory / "nvim.4242.1"
        _dead_nvims_socket(sock)
        with (
            _no_listening_process(),
            patch(
                "vim_ai_follower.backends.nvim_connect.os.kill", side_effect=PermissionError
            ) as kill,
        ):
            assert nvim_connect.probe_nvim(str(sock)) == "silent"
        kill.assert_called_once_with(4242, 0)


@pytest.mark.parametrize(
    "name", ["nvim-@1.sock", "nvim.0.0", "nvim.12x.0", "nvim.12.0.old", "xnvim.12.0"]
)
def test_only_nvims_default_socket_name_with_a_real_pid_is_signalled(name: str) -> None:
    # pid 0 would signal our own process group and read as alive.
    with _short_socket_dir() as directory:
        sock = directory / name
        _dead_nvims_socket(sock)
        with (
            _no_listening_process(),
            patch("vim_ai_follower.backends.nvim_connect.os.kill") as kill,
        ):
            assert nvim_connect.probe_nvim(str(sock)) == "absent"
        kill.assert_not_called()


def test_a_live_default_socket_owner_is_enough_even_when_ps_fails() -> None:
    with _short_socket_dir() as directory:
        sock = directory / f"nvim.{_gone_pid()}.0"
        _dead_nvims_socket(sock)
        with patch(
            "vim_ai_follower.backends.nvim_connect.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout=""),
        ):
            assert nvim_connect.probe_nvim(str(sock)) == "silent"  # still fails closed


class _DyingNvimWorld:
    """subprocess.run for a split or launcher whose nvim binds its socket and
    dies at once (the file stays, nobody listens), with `ps` counted and
    taking `scan_seconds` unless its timeout is shorter (T19 review rm1/rm2)."""

    def __init__(self, sock_path: Path, scan_seconds: float = 0.0) -> None:
        self.sock_path = sock_path
        self.scan_seconds = scan_seconds
        self.scan_timeouts: list[float] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "ps":
            timeout = kwargs["timeout"]
            self.scan_timeouts.append(timeout)
            if timeout < self.scan_seconds:
                time.sleep(timeout)
                raise subprocess.TimeoutExpired(argv, timeout)
            time.sleep(self.scan_seconds)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="")
        _dead_nvims_socket(self.sock_path)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=f"{_SPLIT_PANE}\n")


@contextmanager
def _dying_split(world: _DyingNvimWorld) -> Iterator[None]:
    with (
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=world.sock_path,
        ),
        patch("vim_ai_follower.backends.nvim_connect.subprocess.run", world),
        patch("vim_ai_follower.backends.nvim_connect.TmuxPane") as pane_class,
    ):
        pane_class.return_value.running_command.return_value = None
        yield


def test_the_split_wait_scans_the_processes_at_most_once() -> None:
    # A split nvim that died after binding its socket leaves the file: every
    # 20 ms poll was refused and ran a `ps` scan (42 in one 3.27 s hook).
    with _short_socket_dir() as directory:
        world = _DyingNvimWorld(directory / "nvim-@1.sock")
        with _dying_split(world), pytest.raises(nvim_connect.SplitNvimNeverAnswered):
            nvim_connect.resolve_nvim_target("%1", "@1", adopt=False, wait_seconds=0.5)
    assert len(world.scan_timeouts) == 1


def test_the_split_wait_caps_the_scan_to_the_time_left() -> None:
    # The scan's own 2 s bound made "within N s" take N + 2 s on a loaded
    # machine; capped to what is left, the wait keeps its number.
    with _short_socket_dir() as directory:
        world = _DyingNvimWorld(directory / "nvim-@1.sock", scan_seconds=3.0)
        started = time.monotonic()
        with _dying_split(world), pytest.raises(nvim_connect.SplitNvimNeverAnswered):
            nvim_connect.resolve_nvim_target("%1", "@1", adopt=False, wait_seconds=0.4)
        elapsed = time.monotonic() - started
    assert world.scan_timeouts and max(world.scan_timeouts) <= 0.4
    assert elapsed < 0.4 + 0.5


def test_a_capped_scan_that_runs_out_still_fails_closed() -> None:
    # A scan cut short says nothing about the socket: the probe stays
    # "silent", never "absent", whatever the cap.
    with _short_socket_dir() as directory:
        world = _DyingNvimWorld(directory / "nvim-@1.sock", scan_seconds=3.0)
        with _dying_split(world), pytest.raises(nvim_connect.SplitNvimNeverAnswered):
            nvim_connect.resolve_nvim_target("%1", "@1", adopt=False, wait_seconds=0.3)
        assert nvim_connect.last_probe_verdict(str(world.sock_path)) == "silent"


def test_the_standalone_wait_scans_the_processes_at_most_once() -> None:
    with _short_socket_dir() as directory:
        world = _DyingNvimWorld(directory / "nvim-@1.sock")
        with (
            patch(
                "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
                return_value=world.sock_path,
            ),
            patch("vim_ai_follower.backends.nvim_connect.subprocess.run", world),
            patch(
                "vim_ai_follower.backends.nvim_connect.shutil.which",
                return_value="/usr/bin/nvim-qt",
            ),
            patch("vim_ai_follower.backends.nvim_connect._vimr_app_present", return_value=False),
            pytest.raises(nvim_connect.NvimNeverListened),
        ):
            started = time.monotonic()
            nvim_connect.launch_standalone_nvim("@1", wait_seconds=0.5)
    assert len(world.scan_timeouts) == 1
    assert time.monotonic() - started < 0.5 + 0.5


def test_a_scan_with_no_time_left_fails_closed_without_running_ps() -> None:
    with (
        patch("vim_ai_follower.backends.nvim_connect.subprocess.run") as run,
        nvim_connect._one_scan_until(time.monotonic() - 1.0),
    ):
        assert nvim_connect.is_listened_on_by_a_live_process("/tmp/nvim-@1.sock")
    run.assert_not_called()
