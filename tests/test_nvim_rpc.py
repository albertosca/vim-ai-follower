from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends import get_follower
from vim_ai_follower.backends.nvim_rpc import NvimRpcFollower
from vim_ai_follower.diff import compute_edit_script


def test_is_alive_true_when_connection_succeeds() -> None:
    follower = NvimRpcFollower(socket_path="/tmp/x.sock")
    with patch("vim_ai_follower.backends.nvim_rpc.pynvim.attach", return_value=MagicMock()):
        assert follower.is_alive() is True


def test_is_alive_false_when_socket_missing() -> None:
    follower = NvimRpcFollower(socket_path="/tmp/x.sock")
    with patch(
        "vim_ai_follower.backends.nvim_rpc.pynvim.attach",
        side_effect=OSError("no such file"),
    ):
        assert follower.is_alive() is False


def test_ensure_showing_issues_edit_command() -> None:
    follower = NvimRpcFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    with patch("vim_ai_follower.backends.nvim_rpc.pynvim.attach", return_value=nvim):
        follower.ensure_showing("/tmp/f.txt")
    nvim.command.assert_called_once_with("edit /tmp/f.txt")


def test_apply_edit_sets_buffer_lines_for_each_op_and_reports_completed() -> None:
    follower = NvimRpcFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.backends.nvim_rpc.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.backends.nvim_rpc.time.sleep"),
    ):
        result = follower.apply_edit(
            compute_edit_script("hello\nworld\n", "hello\nvim ai follower\n")
        )
    nvim.current.buffer.__setitem__.assert_any_call(slice(1, 2), ["vim ai follower"])
    assert result == AnimationResult("completed", 1)


def test_show_fresh_renames_buffer_without_edit_then_clears_and_types() -> None:
    follower = NvimRpcFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.backends.nvim_rpc.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.backends.nvim_rpc.time.sleep"),
    ):
        result = follower.show_fresh("/tmp/f.txt", "hello\nworld\n")
    nvim.command.assert_any_call("silent! bwipeout! /tmp/f.txt")
    nvim.command.assert_any_call("file /tmp/f.txt")
    nvim.command.assert_any_call("filetype detect")
    assert "edit /tmp/f.txt" not in [c.args[0] for c in nvim.command.call_args_list]
    nvim.current.buffer.__setitem__.assert_any_call(slice(None, None, None), [])
    nvim.current.buffer.__setitem__.assert_any_call(slice(0, 0), ["hello", "world"])
    assert result == AnimationResult("completed", 1)


def test_goto_line_sets_cursor() -> None:
    follower = NvimRpcFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    with patch("vim_ai_follower.backends.nvim_rpc.pynvim.attach", return_value=nvim):
        follower.goto_line(3)
    assert nvim.current.window.cursor == (3, 0)


def test_stop_is_a_noop() -> None:
    NvimRpcFollower(socket_path="/tmp/x.sock").stop()


def test_get_follower_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        get_follower("bogus", "x")
