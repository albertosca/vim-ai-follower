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
    run.assert_called_once()


def test_resolve_adopts_when_socket_found() -> None:
    with (
        patch(
            "vim_ai_follower.backends.nvim_connect.discover_adopt_socket", return_value="/s.sock"
        ),
    ):
        assert nvim_connect.resolve_nvim_target("%1", "@1", adopt=True) == ("/s.sock", False)


def test_resolve_launches_when_no_adopt(tmp_path: Path) -> None:
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
