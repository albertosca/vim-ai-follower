"""Unit coverage (mocked pynvim) for the name the nvim backend gives the
buffers it names or adds: whatever _DISPLAY_NAME_LUA answers — nvim's own
cwd-relative form of the path when it round-trips through `:p`, the full path
otherwise. The Lua runs inside nvim, so the guard itself is proven against a
real nvim in tests/test_nvim_integration_relative_names.py; here, that one RPC
is the whole naming, and its answer is the name every caller uses."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from helpers import fresh_buffer, route_exec_lua

from vim_ai_follower.backends.nvim import _DISPLAY_NAME_LUA, NvimFollower, _display_name


def _nvim(short: str) -> MagicMock:
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name=short)  # no buffer holds the file yet
    nvim.api.list_tabpages.return_value = []
    nvim.funcs.bufadd.return_value = 42
    return nvim


def test_the_display_name_is_one_rpc_answered_inside_nvim() -> None:
    nvim = MagicMock()
    nvim.exec_lua.return_value = "sub/a.py"
    assert _display_name(nvim, "/proj/sub/a.py") == "sub/a.py"
    nvim.exec_lua.assert_called_once_with(_DISPLAY_NAME_LUA, "/proj/sub/a.py")
    nvim.funcs.fnamemodify.assert_not_called()


def test_the_lua_keeps_the_short_form_only_when_its_p_round_trips() -> None:
    # Spelled out, not rebuilt from the source it checks: this comparison is
    # the guard (see nvim._display_name).
    assert (
        "if vim.fn.fnamemodify(short, ':p') == vim.fn.fnamemodify(full, ':p') then return short end"
    ) in _DISPLAY_NAME_LUA
    assert _DISPLAY_NAME_LUA.strip().endswith("return full")


def test_show_fresh_names_its_buffer_with_the_display_name(tmp_path: Path) -> None:
    nvim = _nvim("sub/a.py")
    buf = fresh_buffer(nvim)
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        NvimFollower(socket_path="/tmp/x.sock", window_id="@1").show_fresh("/p/sub/a.py", "a\n")
    assert nvim.api.buf_set_name.call_args_list == [call(buf, "sub/a.py")]


def test_goto_file_names_the_buffer_it_creates_with_the_display_name() -> None:
    nvim = _nvim("sub/a.py")
    buf = nvim.api.create_buf.return_value
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        NvimFollower(socket_path="/tmp/x.sock").goto_file("/p/sub/a.py")
    assert nvim.api.buf_set_name.call_args_list == [call(buf, "sub/a.py")]


def test_ensure_showing_adds_the_buffer_under_the_display_name() -> None:
    nvim = _nvim("sub/a.py")
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
    ):
        NvimFollower(socket_path="/tmp/x.sock").ensure_showing("/p/sub/a.py")
    nvim.funcs.bufadd.assert_called_once_with("sub/a.py")
