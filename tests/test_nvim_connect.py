from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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
    cmd = nvim_connect.standalone_launch_command("/s.sock", has_nvim_qt=True, has_vimr=True)
    assert cmd == ["nvim-qt", "--", "--listen", "/s.sock"]


def test_standalone_command_uses_vimr_when_no_nvim_qt() -> None:
    cmd = nvim_connect.standalone_launch_command("/s.sock", has_nvim_qt=False, has_vimr=True)
    assert cmd[:3] == ["open", "-a", "VimR"]  # + the file/socket wiring


def test_standalone_command_falls_back_to_terminal_app() -> None:
    cmd = nvim_connect.standalone_launch_command("/s.sock", has_nvim_qt=False, has_vimr=False)
    assert cmd[0] == "osascript"
    assert any("nvim --listen /s.sock" in part for part in cmd)
    # `do script` alone can create an invisible, backgrounded window when
    # Terminal.app is already running (live finding, 2026-09-03: two windows
    # created by `do script` both came back `visible=false` with Terminal not
    # frontmost — nothing appeared on screen even though the command
    # "succeeded"). `activate` is what makes the window actually show up.
    assert any("activate" in part for part in cmd)


def test_launch_standalone_nvim_waits_for_socket_to_appear(tmp_path: Path) -> None:
    sock_path = tmp_path / "nvim-@1.sock"
    poll_count = 0

    class _FakePath:
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
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=sock_path,
        ),
        patch("vim_ai_follower.backends.nvim_connect.subprocess.run") as run,
        patch("vim_ai_follower.backends.nvim_connect.time.sleep") as fake_sleep,
        patch("vim_ai_follower.backends.nvim_connect.Path", side_effect=_FakePath),
        patch("vim_ai_follower.backends.nvim_connect.shutil.which", return_value=None),
        patch("vim_ai_follower.backends.nvim_connect._vimr_app_present", return_value=False),
    ):
        sock = nvim_connect.launch_standalone_nvim("@1")
    assert sock == str(sock_path)
    assert poll_count >= 3
    fake_sleep.assert_called()
    args = run.call_args_list[0].args[0]
    assert args[0] == "osascript"


def test_launch_standalone_nvim_gives_up_after_bounded_wait_when_socket_never_appears(
    tmp_path: Path,
) -> None:
    sock_path = tmp_path / "nvim-@1.sock"  # deliberately never created

    fake_now = [0.0]

    def _fake_monotonic() -> float:
        fake_now[0] += 10.0
        return fake_now[0]

    with (
        patch(
            "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path",
            return_value=sock_path,
        ),
        patch("vim_ai_follower.backends.nvim_connect.subprocess.run"),
        patch("vim_ai_follower.backends.nvim_connect.time.sleep"),
        patch("vim_ai_follower.backends.nvim_connect.time.monotonic", side_effect=_fake_monotonic),
        patch(
            "vim_ai_follower.backends.nvim_connect.shutil.which", return_value="/usr/bin/nvim-qt"
        ),
        patch("vim_ai_follower.backends.nvim_connect._vimr_app_present", return_value=False),
    ):
        sock = nvim_connect.launch_standalone_nvim("@1")
    assert sock == str(sock_path)
    assert not sock_path.exists()


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
