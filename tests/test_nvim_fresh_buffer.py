"""NvimFollower._fresh_buffer, the buffer show_fresh names, reads and types
into: created by handle and checked to be current before anything acts on
the current buffer (final review of backlog-sweep-3, I1), and
_show_in_new_tab, the by-handle new tab every navigation opens (N2/N3, M7).
The real autocmd reproductions are
tests/test_nvim_integration_show_fresh_tab_jump.py."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pynvim
import pytest
from helpers import fresh_buffer, route_exec_lua, tabnew_adds_a_tab

from vim_ai_follower.backends import NavigationFailed
from vim_ai_follower.backends.nvim import NvimFollower


def _follower() -> NvimFollower:
    return NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)


def _mock() -> tuple[MagicMock, MagicMock]:
    nvim = MagicMock()
    buffer = fresh_buffer(nvim)
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    return nvim, buffer


def test_a_landing_on_another_buffer_wipes_the_new_one_and_fails_safe() -> None:
    nvim, buffer = _mock()
    nvim.api.get_current_buf.return_value = MagicMock(name="the user's buffer")
    with pytest.raises(NavigationFailed):
        _follower()._fresh_buffer(nvim, "/tmp/f.py", True)
    nvim.command.assert_any_call(f"silent! bwipeout! {buffer.handle}")


def test_a_tabnew_that_adds_no_tab_fails_safe() -> None:
    nvim, buffer = _mock()
    nvim.api.list_tabpages.side_effect = None
    nvim.api.list_tabpages.return_value = []
    with pytest.raises(NavigationFailed):
        _follower()._fresh_buffer(nvim, "/tmp/f.py", True)
    nvim.api.win_set_buf.assert_not_called()
    nvim.command.assert_any_call(f"silent! bwipeout! {buffer.handle}")


def test_the_empty_buffer_the_tabnew_made_is_wiped_once_replaced() -> None:
    nvim, _buffer = _mock()
    scratch = MagicMock(handle=9)
    nvim.api.win_get_buf.return_value = scratch
    nvim.api.buf_get_name.return_value = ""
    nvim.api.buf_get_option.return_value = False
    nvim.api.buf_line_count.return_value = 1
    nvim.api.buf_get_lines.return_value = [""]
    nvim.funcs.win_findbuf.return_value = []
    assert _follower()._fresh_buffer(nvim, "/tmp/f.py", True) == 7
    nvim.command.assert_any_call("silent! bwipeout 9")


@pytest.mark.parametrize(
    ("name", "lines", "windows"),
    [("/tmp/named.py", [""], []), ("", ["typed"], []), ("", [""], [1001])],
    ids=["named", "not-empty", "still-shown"],
)
def test_a_scratch_buffer_that_is_not_empty_unnamed_and_hidden_stays(
    name: str, lines: list[str], windows: list[int]
) -> None:
    nvim, _buffer = _mock()
    nvim.api.win_get_buf.return_value = MagicMock(handle=9)
    nvim.api.buf_get_name.return_value = name
    nvim.api.buf_get_option.return_value = False
    nvim.api.buf_line_count.return_value = 1
    nvim.api.buf_get_lines.return_value = lines
    nvim.funcs.win_findbuf.return_value = windows
    _follower()._fresh_buffer(nvim, "/tmp/f.py", True)
    assert call("silent! bwipeout 9") not in nvim.command.call_args_list


def test_without_a_new_tab_the_buffer_goes_in_the_current_window() -> None:
    nvim, buffer = _mock()
    with patch.object(nvim.api, "list_tabpages") as tabs:
        assert _follower()._fresh_buffer(nvim, "/tmp/f.py", False) == 7
    tabs.assert_not_called()
    assert call("tabnew") not in nvim.command.call_args_list
    nvim.api.win_set_buf.assert_called_once_with(
        nvim.api.get_current_win.return_value, buffer.handle
    )


def test_a_refused_switch_is_navigation_failed_and_wipes_the_new_buffer() -> None:
    """Under 'nohidden' nvim refuses to replace a window's modified buffer
    (E37): a NavigationFailed the hooks log, never a raw NvimError."""
    nvim, buffer = _mock()
    nvim.api.win_set_buf.side_effect = pynvim.NvimError("Vim:E37: No write since last change")
    with pytest.raises(NavigationFailed, match="E37"):
        _follower()._fresh_buffer(nvim, "/tmp/f.py", True)
    nvim.command.assert_any_call(f"silent! bwipeout! {buffer.handle}")


def test_a_failed_new_tab_never_wipes_a_buffer_it_did_not_create() -> None:
    """goto_file's "buffer exists but is shown nowhere" branch: the buffer
    may be the user's, so a landing elsewhere wipes nothing."""
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=5, display_name="/tmp/f.py")
    tabnew_adds_a_tab(nvim)
    nvim.api.get_current_buf.side_effect = None
    nvim.api.get_current_buf.return_value = MagicMock(handle=1)
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        pytest.raises(NavigationFailed),
    ):
        _follower().goto_file("/tmp/f.py")
    assert [entry for entry in nvim.command.call_args_list if "bwipeout" in entry.args[0]] == []
    nvim.api.win_set_var.assert_not_called()


def test_a_read_that_does_not_land_wipes_the_buffer_it_loaded() -> None:
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    tabnew_adds_a_tab(nvim)
    nvim.funcs.bufadd.return_value = 42
    nvim.api.get_current_buf.side_effect = None
    nvim.api.get_current_buf.return_value = MagicMock(handle=1)
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        pytest.raises(NavigationFailed),
    ):
        _follower().ensure_showing("/tmp/f.py")
    nvim.command.assert_any_call("silent! bwipeout! 42")


def _hijacked(window_valid: bool, now_name: str) -> tuple[MagicMock, MagicMock]:
    """A tabnew under a TabNew jump: the user's window 1000 showed buffer 1
    and now shows buffer 3 (named `now_name`); the added tab's window shows
    buffer 1 (the split copy)."""
    nvim = MagicMock()
    user_buffer, now = MagicMock(handle=1), MagicMock(handle=3)
    user_window, added = MagicMock(handle=1000), MagicMock(handle=1001)
    nvim.api.list_wins.return_value = [user_window]
    nvim.api.win_is_valid.return_value = window_valid
    nvim.api.buf_get_name.side_effect = lambda buffer: now_name if buffer is now else "x"
    nvim.api.buf_get_option.return_value = False
    nvim.api.buf_is_loaded.return_value = True
    nvim.api.buf_line_count.return_value = 1
    nvim.api.buf_get_lines.return_value = [""]
    nvim.funcs.win_findbuf.return_value = []
    shown = {1000: user_buffer, 1001: user_buffer}
    calls = {"count": 0}

    def win_get_buf(window: object) -> MagicMock:
        handle = getattr(window, "handle", window)
        calls["count"] += 1
        # The first read is the snapshot before the tabnew.
        if handle == 1000 and calls["count"] > 1:
            return now
        return shown[handle]  # type: ignore[index]

    nvim.api.win_get_buf.side_effect = win_get_buf
    return nvim, added


def test_a_window_an_early_jump_hijacked_gets_the_users_buffer_back() -> None:
    from vim_ai_follower.backends.nvim import _give_back_hijacked_windows

    nvim, added = _hijacked(window_valid=True, now_name="")
    _give_back_hijacked_windows(nvim, {1000: 1}, added)
    nvim.api.win_set_buf.assert_called_once_with(1000, 1)
    nvim.command.assert_called_once_with("silent! bwipeout 3")


@pytest.mark.parametrize(
    ("window_valid", "now_name"),
    [(False, ""), (True, "/tmp/theirs.py")],
    ids=["window-gone", "not-a-scratch"],
)
def test_a_window_that_was_not_hijacked_is_left_alone(window_valid: bool, now_name: str) -> None:
    from vim_ai_follower.backends.nvim import _give_back_hijacked_windows

    nvim, added = _hijacked(window_valid, now_name)
    _give_back_hijacked_windows(nvim, {1000: 1}, added)
    nvim.api.win_set_buf.assert_not_called()
    nvim.command.assert_not_called()


def test_a_give_back_nvim_refuses_is_navigation_failed() -> None:
    nvim, buffer = _mock()
    nvim.api.list_wins.return_value = [MagicMock(handle=1000)]
    nvim.api.win_get_buf.return_value = MagicMock(handle=1)
    nvim.api.win_is_valid.side_effect = pynvim.NvimError("Invalid window id")
    with pytest.raises(NavigationFailed, match="Invalid window id"):
        _follower()._fresh_buffer(nvim, "/tmp/f.py", True)
    nvim.command.assert_any_call(f"silent! bwipeout! {buffer.handle}")
