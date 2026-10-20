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
from vim_ai_follower.state import FollowerState


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
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
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
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        pytest.raises(NavigationFailed),
    ):
        _follower().ensure_showing("/tmp/f.py")
    nvim.command.assert_any_call("silent! bwipeout! 42")


def _after_tabnew(
    now: dict[int, MagicMock], valid_windows: set[int], valid_buffers: set[int]
) -> MagicMock:
    """A mock nvim after an early-jumping `:tabnew`: window handle -> the
    buffer it shows NOW; only the named handles are valid. A buffer named
    "" is the tabnew's empty scratch, any other name is a user's buffer."""
    nvim = MagicMock()
    nvim.api.win_is_valid.side_effect = lambda handle: handle in valid_windows
    nvim.api.buf_is_valid.side_effect = lambda handle: handle in valid_buffers
    nvim.api.win_get_buf.side_effect = lambda handle: now[handle]
    nvim.api.buf_get_name.side_effect = lambda buffer: buffer.name_for_test
    nvim.api.buf_get_option.return_value = False
    nvim.api.buf_is_loaded.return_value = True
    nvim.api.buf_line_count.return_value = 1
    nvim.api.buf_get_lines.return_value = [""]
    nvim.funcs.win_findbuf.return_value = []
    return nvim


def _buffer(handle: int, name: str) -> MagicMock:
    buffer = MagicMock(handle=handle)
    buffer.name_for_test = name
    return buffer


def test_every_window_an_early_jump_hijacked_gets_its_buffer_back_in_any_tab() -> None:
    """Review of sweep-4 T8, I2: the user sat in tab 2 (window 2000, buffer
    2) and `TabNew * tabfirst` dropped the scratch (buffer 9) into tab 1's
    window 1000, which showed buffer 1. Window 3000 was not touched."""
    from vim_ai_follower.backends.nvim import _give_back_hijacked_windows

    scratch = _buffer(9, "")
    nvim = _after_tabnew(
        {1000: scratch, 2000: _buffer(2, "b.txt"), 3000: _buffer(3, "c.txt")},
        valid_windows={1000, 2000, 3000},
        valid_buffers={1, 2, 3, 9},
    )
    _give_back_hijacked_windows(nvim, {1000: 1, 2000: 2, 3000: 3})
    nvim.api.win_set_buf.assert_called_once_with(1000, 1)
    nvim.command.assert_called_once_with("silent! bwipeout 9")


@pytest.mark.parametrize(
    ("valid_windows", "valid_buffers", "now_name"),
    [({2000}, {1, 9}, ""), ({1000}, {9}, ""), ({1000}, {1, 9}, "/tmp/theirs.py")],
    ids=["window-gone", "buffer-gone", "not-a-scratch"],
)
def test_a_window_that_was_not_hijacked_is_left_alone(
    valid_windows: set[int], valid_buffers: set[int], now_name: str
) -> None:
    from vim_ai_follower.backends.nvim import _give_back_hijacked_windows

    nvim = _after_tabnew({1000: _buffer(9, now_name)}, valid_windows, valid_buffers)
    _give_back_hijacked_windows(nvim, {1000: 1})
    nvim.api.win_set_buf.assert_not_called()
    nvim.command.assert_not_called()


def test_a_window_still_showing_its_own_empty_buffer_is_left_alone() -> None:
    """The user's window showed their own empty [No Name] (buffer 1) and
    still does: nothing to give back, nothing to wipe."""
    from vim_ai_follower.backends.nvim import _give_back_hijacked_windows

    nvim = _after_tabnew({1000: _buffer(1, "")}, {1000}, {1})
    _give_back_hijacked_windows(nvim, {1000: 1})
    nvim.api.win_set_buf.assert_not_called()
    nvim.command.assert_not_called()


def test_an_unloaded_unnamed_buffer_is_a_scratch() -> None:
    """Under 'nohidden' the replaced scratch is unloaded, and an unloaded
    buffer has no lines (buf_line_count 0): it still counts as empty."""
    from vim_ai_follower.backends.nvim import _is_scratch

    nvim = MagicMock()
    nvim.api.buf_get_name.return_value = ""
    nvim.api.buf_get_option.return_value = False
    nvim.api.buf_is_loaded.return_value = False
    nvim.api.buf_line_count.return_value = 0
    nvim.api.buf_get_lines.return_value = []
    assert _is_scratch(nvim, MagicMock(handle=9)) is True
    nvim.api.buf_is_loaded.return_value = True
    assert _is_scratch(nvim, MagicMock(handle=9)) is False


def test_a_give_back_nvim_refuses_is_navigation_failed() -> None:
    nvim, buffer = _mock()
    nvim.api.list_wins.return_value = [MagicMock(handle=1000)]
    nvim.api.win_get_buf.return_value = MagicMock(handle=1)
    nvim.api.win_is_valid.side_effect = pynvim.NvimError("Invalid window id")
    with pytest.raises(NavigationFailed, match="Invalid window id"):
        _follower()._fresh_buffer(nvim, "/tmp/f.py", True)
    nvim.command.assert_any_call(f"silent! bwipeout! {buffer.handle}")


@pytest.mark.parametrize(
    ("state", "opens_tab"),
    [("dedicated", False), ("adopted", True), ("gone", True)],
)
def test_only_a_proven_dedicated_follower_types_a_first_file_in_place(
    state: str, opens_tab: bool
) -> None:
    """Review of sweep-4 T8, I1: the hook asks for the current window
    (in_new_tab=shown_any=False) on an adopted nvim's first Write; the
    window there is the user's, so show_fresh opens a tab anyway, and so
    does an nvim whose state is gone (fail closed)."""
    if state != "gone":
        FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=state == "adopted")
    nvim, _buffer = _mock()
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        _follower().show_fresh("/tmp/f.py", "a\n", in_new_tab=False)
    assert (call("tabnew") in nvim.command.call_args_list) is opens_tab


def test_a_replaced_buffer_its_own_bufhidden_wiped_is_left_alone() -> None:
    """Task 24 item 1: a start screen's buffer carries bufhidden=wipe, so nvim
    wipes it the moment show_fresh replaces it in the window, and asking a
    wiped buffer its name raised "Invalid buffer id" out of the first Edit.
    A buffer that is already gone is not inspected and not wiped again."""
    nvim, _buffer = _mock()
    nvim.api.win_get_buf.return_value = MagicMock(handle=1)
    nvim.api.buf_is_valid.return_value = False
    nvim.api.buf_get_name.side_effect = pynvim.NvimError("Invalid buffer id: 1")
    assert _follower()._fresh_buffer(nvim, "/tmp/f.py", False) == 7
    nvim.api.buf_is_valid.assert_any_call(1)
    assert call("silent! bwipeout 1") not in nvim.command.call_args_list


@pytest.mark.parametrize("original_valid", [True, False], ids=["original-kept", "original-gone"])
def test_a_failed_new_tab_is_taken_back_by_handle(original_valid: bool) -> None:
    """T8 matrix m1/m3: every tab not in the record is closed (forced, so a
    modified buffer is hidden, never discarded), its empty scratch wiped,
    and the window that was current goes back to being current, unless it
    is gone. A close nvim refuses does not stop the rest."""
    from vim_ai_follower.backends.nvim import _undo_new_tab

    old_tab, new_tab = MagicMock(handle=1), MagicMock(handle=2)
    refused, closed = MagicMock(handle=2001), MagicMock(handle=2002)
    scratch = _buffer(9, "")
    nvim = _after_tabnew({2001: scratch, 2002: scratch}, {1000}, set())
    nvim.api.list_tabpages.return_value = [old_tab, new_tab]
    nvim.api.tabpage_list_wins.side_effect = lambda tab: [refused, closed] if tab is new_tab else []
    nvim.api.win_get_buf.side_effect = lambda window: scratch

    def win_close(window: MagicMock, force: bool) -> None:
        if window is refused:
            raise pynvim.NvimError("E444: Cannot close last window")

    nvim.api.win_close.side_effect = win_close
    nvim.api.buf_is_valid.side_effect = lambda handle: handle == 9
    nvim.api.win_is_valid.side_effect = lambda handle: original_valid and handle == 1000
    _undo_new_tab(nvim, {1}, {}, 1000)
    assert nvim.api.win_close.call_args_list == [call(refused, True), call(closed, True)]
    nvim.command.assert_called_once_with("silent! bwipeout 9")
    assert nvim.api.set_current_win.call_args_list == ([call(1000)] if original_valid else [])


def test_a_tabnew_that_throws_is_navigation_failed_and_taken_back() -> None:
    nvim, buffer = _mock()

    def command(text: str) -> None:
        if text == "tabnew":
            raise pynvim.NvimError('TabNew Autocommands for "*": user TabNew failed')

    nvim.command.side_effect = command
    with pytest.raises(NavigationFailed, match="user TabNew failed"):
        _follower()._fresh_buffer(nvim, "/tmp/f.py", True)
    nvim.api.win_set_buf.assert_not_called()
    nvim.api.tabpage_list_wins.assert_called()
    nvim.command.assert_any_call(f"silent! bwipeout! {buffer.handle}")
