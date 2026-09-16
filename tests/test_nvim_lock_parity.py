"""Unit coverage (mocked pynvim) for lock parity between the nvim backend's
disk-reading entry points and the tmux backend's `ensure_showing`, which
always relocks (`:setlocal readonly nomodifiable`) after `:tab drop` — even
for an adopted (user-owned) Vim. On nvim, `ensure_showing` (both the
existing-buffer and the `_open_from_disk` branches) and `apply_edit`'s
vanished-buffer branch (which also goes through `_open_from_disk`) must
apply the equivalent lock: the exact `buf_set_option(buf, "modifiable",
False)` call `_drive`'s completion relock uses, applied unconditionally
(no `_is_adopted` check, mirroring tmux's own unconditional lock)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from vim_ai_follower import state
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends.nvim import NvimFollower
from vim_ai_follower.diff import EditOp


def test_ensure_showing_locks_an_existing_buffer_after_switching_to_it() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = 9
    nvim.api.get_current_buf.return_value.handle = 9
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        follower.ensure_showing("/tmp/f.py")
    goto_file.assert_called_once_with("/tmp/f.py")
    nvim.api.buf_set_option.assert_called_once_with(9, "modifiable", False)


def test_ensure_showing_locks_unconditionally_without_checking_prior_state() -> None:
    # hand_over (or an in-progress edit interrupted) leaves a buffer
    # modifiable; a later Read-navigation back to it must relock it, exactly
    # like tmux's ensure_showing unconditionally resends `_LOCK_READONLY`
    # rather than first querying the current lock state.
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = 9
    nvim.api.get_current_buf.return_value.handle = 9
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        follower.ensure_showing("/tmp/f.py")
    goto_file.assert_called_once_with("/tmp/f.py")
    nvim.api.buf_get_option.assert_not_called()
    assert call(9, "modifiable", False) in nvim.api.buf_set_option.call_args_list


def test_ensure_showing_locks_an_adopted_followers_buffer_too(tmp_path: Path) -> None:
    # tmux locks the user's own adopted Vim the same as a dedicated one;
    # nvim's ensure_showing must not special-case adopted here (unlike
    # _drive's completion relock, which does).
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = 9
    nvim.api.get_current_buf.return_value.handle = 9
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=True)
        follower.ensure_showing("/tmp/f.py")
    nvim.api.buf_set_option.assert_called_once_with(9, "modifiable", False)


def test_ensure_showing_locks_a_disk_loaded_buffer_for_a_never_seen_file() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = -1
    nvim.funcs.bufadd.return_value = 42
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        follower.ensure_showing("/tmp/f.py")
    goto_file.assert_not_called()
    assert nvim.api.buf_set_option.call_args_list == [
        call(42, "buflisted", True),
        call(42, "modifiable", False),
    ]


def test_apply_edit_locks_the_disk_loaded_buffer_when_the_original_vanished() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = -1
    nvim.funcs.bufadd.return_value = 42
    ops = [EditOp(kind="replace", start_line=3, end_line=3, new_lines=("C",))]
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        result = follower.apply_edit("/tmp/f.py", ops)
    assert result == AnimationResult("completed", 1)
    goto_file.assert_not_called()
    assert nvim.api.buf_set_option.call_args_list == [
        call(42, "buflisted", True),
        call(42, "modifiable", False),
    ]


def test_goto_file_itself_never_touches_modifiable() -> None:
    # The lock lives in ensure_showing/_open_from_disk, never in goto_file:
    # show_fresh's callers unlock right after it anyway, and _drive owns
    # that lifecycle end to end.
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = 9
    tab, win = MagicMock(), MagicMock()
    buf = MagicMock(number=9)
    nvim.api.list_tabpages.return_value = [tab]
    nvim.api.tabpage_list_wins.return_value = [win]
    nvim.api.win_get_buf.return_value = buf
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.goto_file("/tmp/f.py")
    nvim.api.buf_set_option.assert_not_called()
