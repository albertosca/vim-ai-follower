from __future__ import annotations

import shutil
import socket
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower.backends import nvim_connect


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
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen()
        try:
            launcher = MagicMock()
            with _patched_launcher(sock_path, launcher):
                assert nvim_connect.launch_standalone_nvim("@1") == str(sock_path)
            assert sock_path.is_socket()
            # Adopted, not relaunched: a second window's nvim would die on
            # "address already in use" and stay on screen as an orphan.
            launcher.assert_not_called()
        finally:
            server.close()


def test_launch_standalone_nvim_attaches_once_the_socket_accepts(tmp_path: Path) -> None:
    sock_path = tmp_path / "nvim-@1.sock"
    with (
        _patched_launcher(sock_path, lambda *_a, **_k: None),
        patch(
            "vim_ai_follower.backends.nvim_connect._answers",
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
        patch("vim_ai_follower.backends.nvim_connect._answers", side_effect=[False, True, True]),
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
        patch("vim_ai_follower.backends.nvim_connect._answers", side_effect=[False, True, True]),
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
