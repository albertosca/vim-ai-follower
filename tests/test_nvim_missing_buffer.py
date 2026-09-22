"""Unit coverage (mocked pynvim) for the nvim backend's "the buffer is not
there" paths: Read-navigation to a never-seen file, an out-of-range Read
offset, and an edit whose buffer vanished since the snapshot was taken."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends.nvim import NvimFollower
from vim_ai_follower.control import PendingApplyEdit, PendingShowFresh
from vim_ai_follower.diff import EditOp


def test_ensure_showing_switches_to_an_existing_buffer_without_touching_disk() -> None:
    # An existing buffer may hold typed-but-unsaved content (this backend
    # never writes its buffers), so it is switched to, never reloaded.
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.exec_lua.return_value = 9
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        follower.ensure_showing("/tmp/f.py")
    goto_file.assert_called_once_with("/tmp/f.py")
    nvim.funcs.bufload.assert_not_called()


def test_ensure_showing_loads_the_real_disk_content_when_no_buffer_exists() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.exec_lua.return_value = -1
    nvim.funcs.bufadd.return_value = 42
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        follower.ensure_showing("/tmp/f.py")
    goto_file.assert_not_called()
    nvim.funcs.bufadd.assert_called_once_with("/tmp/f.py")
    nvim.funcs.bufload.assert_called_once_with(42)
    # swapfile off first (before bufload, so a live second editor's swap
    # cannot raise E325 — see test_nvim_swap.py), then buflisted, then
    # locked (nomodifiable) — tmux-parity lock (see test_nvim_lock_parity.py
    # for the dedicated coverage).
    assert nvim.api.buf_set_option.call_args_list == [
        call(42, "swapfile", False),
        call(42, "buflisted", True),
        call(42, "modifiable", False),
    ]
    assert [c.args[0] for c in nvim.command.call_args_list] == ["tabnew", "filetype detect"]
    nvim.api.win_set_buf.assert_called_once_with(0, 42)


def test_goto_line_clamps_an_offset_past_the_last_line() -> None:
    # An out-of-range Read offset is normal (the file can be shorter than the
    # offset Claude sent); nvim's cursor setter raises on it.
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.api.buf_line_count.return_value = 2
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.goto_line(99)
    assert nvim.current.window.cursor == (2, 0)


def test_goto_line_keeps_an_in_range_offset() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.api.buf_line_count.return_value = 10
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.goto_line(3)
    assert nvim.current.window.cursor == (3, 0)


def test_apply_edit_shows_disk_content_instead_of_animating_into_nothing() -> None:
    # The ops were computed against a snapshot of a buffer that no longer
    # exists; animating them into a fresh empty buffer either fabricates
    # wrong content or raises "Index out of bounds".
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.exec_lua.return_value = -1
    nvim.funcs.bufadd.return_value = 42
    ops = [
        EditOp(kind="replace", start_line=5, end_line=5, new_lines=("E",)),
        EditOp(kind="replace", start_line=3, end_line=3, new_lines=("C",)),
    ]
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        result = follower.apply_edit("/tmp/f.py", ops)
    assert result == AnimationResult("completed", 2)
    goto_file.assert_not_called()
    nvim.funcs.bufload.assert_called_once_with(42)
    nvim.api.win_set_buf.assert_called_once_with(0, 42)
    # nothing was animated into the freshly loaded buffer
    nvim.api.buf_set_lines.assert_not_called()
    nvim.api.buf_set_text.assert_not_called()


def test_resume_apply_edit_returns_completed_and_touches_nothing_when_the_buffer_vanished() -> None:
    # Deliberately NOT a disk load: leaving the buffer absent is what lets the
    # apply_edit that follows in hooks._animate_edit take its own guard.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.exec_lua.return_value = -1
    pending = PendingApplyEdit(
        ops=[
            EditOp(kind="replace", start_line=5, end_line=5, new_lines=("E",)),
            EditOp(kind="replace", start_line=3, end_line=3, new_lines=("C",)),
        ],
        pace_seconds=0.0,
        file_path="/tmp/f.py",
    )
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        result = follower.resume(pending)
    assert result == AnimationResult("completed", 2)
    goto_file.assert_not_called()
    nvim.funcs.bufadd.assert_not_called()
    nvim.funcs.bufload.assert_not_called()
    nvim.api.create_buf.assert_not_called()
    nvim.api.buf_set_lines.assert_not_called()
    nvim.command.assert_not_called()


def test_resume_show_fresh_returns_completed_and_touches_nothing_when_the_buffer_vanished() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.exec_lua.return_value = -1
    pending = PendingShowFresh(
        lines=("one", "two", "three"), pace_seconds=0.0, continuation=True, file_path="/tmp/f.py"
    )
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        result = follower.resume(pending, seeded=True)
    assert result == AnimationResult("completed", 3)
    goto_file.assert_not_called()
    nvim.api.create_buf.assert_not_called()
    nvim.api.buf_set_lines.assert_not_called()
    nvim.command.assert_not_called()


def test_apply_edit_animates_normally_when_the_buffer_is_still_there(tmp_path: Path) -> None:
    # The guard must not divert a healthy edit to the disk-load path.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.exec_lua.return_value = 9
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.api.buf_line_count.return_value = 1
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("completed", 1)
    goto_file.assert_called_once_with("/tmp/f.py")
    nvim.funcs.bufload.assert_not_called()
    nvim.api.buf_set_text.assert_any_call(7, 0, 0, 0, 0, ["a"])
