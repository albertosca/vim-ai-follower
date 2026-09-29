"""Unit coverage for the nvim Read's re-read of a clean open buffer (see
tests/test_nvim_integration_read_reload.py for the real-nvim behavior): the
clean check, the `silent edit!` on each exec path, and the changedtick stamp
a completed animation leaves."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from vim_ai_follower import state
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends.nvim import _IS_CLEAN_LUA, NvimFollower


def _nvim(*, clean: bool, swap: bool) -> MagicMock:
    nvim = MagicMock()
    nvim.exec_lua.side_effect = lambda code, *args: clean if code == _IS_CLEAN_LUA else 9
    nvim.api.get_current_buf.return_value.handle = 9
    nvim.api.buf_get_option.return_value = swap
    nvim.api.exec2.return_value = {"output": ""}
    return nvim


def _ensure_showing(nvim: MagicMock, tmp_path: Path, *, adopted: bool) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=adopted)
        follower.ensure_showing("/tmp/f.py")


def _edits(nvim: MagicMock) -> list[str]:
    """Every `edit!` the follower ran, through either exec path."""
    commands = [c.args[0] for c in nvim.command.call_args_list]
    commands += [c.args[0] for c in nvim.api.exec2.call_args_list]
    return [c for c in commands if "edit!" in c]


def test_a_clean_buffer_is_re_read_silently(tmp_path: Path) -> None:
    nvim = _nvim(clean=True, swap=False)
    _ensure_showing(nvim, tmp_path, adopted=False)
    assert _edits(nvim) == ["silent edit!"]


def test_a_buffer_that_is_not_clean_is_never_re_read(tmp_path: Path) -> None:
    nvim = _nvim(clean=False, swap=True)
    _ensure_showing(nvim, tmp_path, adopted=True)
    assert _edits(nvim) == []
    nvim.api.buf_get_option.assert_not_called()


def test_an_adopted_nvim_re_reads_plainly_and_keeps_its_swap(tmp_path: Path) -> None:
    """No swap toggle around the re-read: nvim's `:edit!` keeps a buffer's
    existing swap and runs no new swap search (see _reload_if_clean)."""
    nvim = _nvim(clean=True, swap=True)
    _ensure_showing(nvim, tmp_path, adopted=True)
    assert nvim.command.call_args_list == [call("silent edit!")]
    assert "swapfile" not in [c.args[1] for c in nvim.api.buf_set_option.call_args_list]


def _drive(outcome: str) -> MagicMock:
    nvim = MagicMock()
    nvim.api.buf_get_changedtick.return_value = 42
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    with (
        patch("vim_ai_follower.control.mark_animating"),
        patch("vim_ai_follower.control.clear_animating"),
    ):
        follower._drive(nvim, 7, lambda: AnimationResult(outcome, 1))  # type: ignore[arg-type]
    return nvim


def test_a_completed_animation_stamps_the_changedtick() -> None:
    nvim = _drive("completed")
    assert call(7, "vaf_synced_tick", 42) in nvim.api.buf_set_var.call_args_list


def test_an_interrupted_animation_leaves_no_stamp() -> None:
    """The handed-over buffer is the user's: a Read must not re-read it."""
    nvim = _drive("interrupted")
    nvim.api.buf_set_var.assert_not_called()


# ---- Follower.user_readonly (the hand-off cue's `:w!` variant)


def _user_readonly(nvim: MagicMock, tmp_path: Path, *, adopted: bool) -> bool:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=adopted)
        return follower.user_readonly("/tmp/f.py")


def test_an_adopted_nvims_readonly_buffer_is_the_users(tmp_path: Path) -> None:
    nvim = MagicMock()
    nvim.exec_lua.return_value = 9
    nvim.api.buf_get_option.return_value = True
    assert _user_readonly(nvim, tmp_path, adopted=True) is True
    nvim.api.buf_get_option.assert_called_once_with(9, "readonly")


def test_a_dedicated_nvim_never_asks(tmp_path: Path) -> None:
    nvim = MagicMock()
    assert _user_readonly(nvim, tmp_path, adopted=False) is False
    nvim.exec_lua.assert_not_called()


def test_a_missing_buffer_or_a_dead_nvim_is_not_readonly(tmp_path: Path) -> None:
    nvim = MagicMock()
    nvim.exec_lua.return_value = -1
    assert _user_readonly(nvim, tmp_path, adopted=True) is False
    nvim.exec_lua.side_effect = OSError("gone")
    assert _user_readonly(nvim, tmp_path, adopted=True) is False
