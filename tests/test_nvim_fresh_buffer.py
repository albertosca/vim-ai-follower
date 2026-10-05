"""NvimFollower._fresh_buffer, the buffer show_fresh names, reads and types
into: created by handle and checked to be current before anything acts on
the current buffer (final review of backlog-sweep-3, I1). The real autocmd
reproduction is tests/test_nvim_integration_show_fresh_tab_jump.py."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from helpers import fresh_buffer, route_exec_lua

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
    nvim.api.win_set_buf.assert_called_once_with(nvim.api.get_current_win.return_value, buffer)
