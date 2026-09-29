"""Unit coverage (mocked pynvim) for the name the nvim backend gives the
buffers it names or adds: nvim's own cwd-relative form of the path
(`fnamemodify(path, ':.')`), except a relative form starting with `~`, which
stays full. tests/test_nvim_integration_relative_names.py proves the names
against a real nvim."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from vim_ai_follower.backends.nvim import NvimFollower, _display_name


def _nvim(short: str) -> MagicMock:
    nvim = MagicMock()
    nvim.funcs.fnamemodify.return_value = short
    nvim.exec_lua.return_value = -1  # no buffer holds the file yet
    nvim.api.list_tabpages.return_value = []
    nvim.funcs.bufadd.return_value = 42
    return nvim


def test_the_display_name_is_nvims_cwd_relative_form() -> None:
    nvim = _nvim("sub/a.py")
    assert _display_name(nvim, "/proj/sub/a.py") == "sub/a.py"
    nvim.funcs.fnamemodify.assert_called_once_with("/proj/sub/a.py", ":.")


def test_a_relative_form_starting_with_a_tilde_stays_full() -> None:
    assert _display_name(_nvim("~x/a.py"), "/proj/~x/a.py") == "/proj/~x/a.py"


def test_show_fresh_names_its_buffer_with_the_display_name(tmp_path: Path) -> None:
    nvim = _nvim("sub/a.py")
    buf = nvim.current.buffer
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
