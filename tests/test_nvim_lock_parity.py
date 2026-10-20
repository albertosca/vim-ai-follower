"""Unit coverage (mocked pynvim) for the nvim backend's navigation lock: a
launched, dedicated follower's buffer gets relocked (nomodifiable) by
`ensure_showing` (both the existing-buffer and the `_open_from_disk`
branches) and by `apply_edit`'s vanished-buffer branch (which also goes
through `_open_from_disk`) — the exact `buf_set_option(buf, "modifiable",
False)` call `_drive`'s completion relock uses. An ADOPTED nvim is the
user's own editor and is never locked by this backend, navigation included
(the same fail-closed `_locks_buffers` guard `_drive` uses) — unlike the tmux backend, which
locks an adopted Vim's tab the same as a dedicated one. A prior controller
ruling (2026-09-16) made this lock unconditional to match tmux exactly;
that ruling was reversed, so this file covers both the launched case (lock
applies) and the adopted case (lock never applies) for every branch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from helpers import route_exec_lua, tabnew_adds_a_tab

from vim_ai_follower import state
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends.nvim import _IS_CLEAN_LUA, NvimFollower
from vim_ai_follower.diff import EditOp


def _buffer_9_not_clean(code: str, *arguments: object) -> object:
    """exec_lua for an existing buffer 9 that is NOT clean, so ensure_showing
    switches to it without a re-read: these tests are about the lock only
    (the re-read has its own in tests/test_nvim_read_reload.py)."""
    return False if code == _IS_CLEAN_LUA else 9


def test_ensure_showing_locks_an_existing_buffer_after_switching_to_it() -> None:
    state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=False)  # proven dedicated
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.exec_lua.side_effect = _buffer_9_not_clean
    nvim.api.get_current_buf.return_value.handle = 9
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        follower.ensure_showing("/tmp/f.py")
    goto_file.assert_called_once_with("/tmp/f.py")
    nvim.api.buf_set_option.assert_called_once_with(9, "modifiable", False)


def test_ensure_showing_locks_unconditionally_without_checking_prior_state() -> None:
    state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=False)  # proven dedicated
    # hand_over (or an in-progress edit interrupted) leaves a buffer
    # modifiable; a later Read-navigation back to it must relock it, exactly
    # like tmux's ensure_showing unconditionally resends `_LOCK_READONLY`
    # rather than first querying the current lock state.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.exec_lua.side_effect = _buffer_9_not_clean
    nvim.api.get_current_buf.return_value.handle = 9
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        follower.ensure_showing("/tmp/f.py")
    goto_file.assert_called_once_with("/tmp/f.py")
    nvim.api.buf_get_option.assert_not_called()
    assert call(9, "modifiable", False) in nvim.api.buf_set_option.call_args_list


def test_ensure_showing_leaves_an_adopted_followers_existing_buffer_modifiable(
    tmp_path: Path,
) -> None:
    # Unlike tmux (which locks an adopted Vim's tab the same as a dedicated
    # one), nvim's ensure_showing must special-case adopted here, exactly
    # like _drive's completion relock does: the user's own editor is never
    # locked by this backend.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.exec_lua.side_effect = _buffer_9_not_clean
    nvim.api.get_current_buf.return_value.handle = 9
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=True)
        follower.ensure_showing("/tmp/f.py")
    nvim.api.buf_set_option.assert_not_called()


def test_ensure_showing_leaves_an_adopted_followers_disk_loaded_buffer_modifiable(
    tmp_path: Path,
) -> None:
    # The _open_from_disk branch (never-seen file) must apply the same
    # adopted guard as the existing-buffer branch above.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    tabnew_adds_a_tab(nvim)
    nvim.funcs.bufadd.return_value = 42
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=True)
        follower.ensure_showing("/tmp/f.py")
    goto_file.assert_not_called()
    assert nvim.api.buf_set_option.call_args_list == [
        call(42, "swapfile", False),
        call(42, "buflisted", True),
    ]


def test_apply_edit_leaves_an_adopted_followers_disk_loaded_buffer_modifiable(
    tmp_path: Path,
) -> None:
    # apply_edit's vanished-buffer branch also goes through _open_from_disk
    # and must apply the same adopted guard.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    tabnew_adds_a_tab(nvim)
    nvim.funcs.bufadd.return_value = 42
    ops = [EditOp(kind="replace", start_line=3, end_line=3, new_lines=("C",))]
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=True)
        result = follower.apply_edit("/tmp/f.py", ops)
    assert result == AnimationResult("completed", 1)
    goto_file.assert_not_called()
    assert nvim.api.buf_set_option.call_args_list == [
        call(42, "swapfile", False),
        call(42, "buflisted", True),
    ]


def test_ensure_showing_locks_a_disk_loaded_buffer_for_a_never_seen_file() -> None:
    state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=False)  # proven dedicated
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    tabnew_adds_a_tab(nvim)
    nvim.funcs.bufadd.return_value = 42
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        follower.ensure_showing("/tmp/f.py")
    goto_file.assert_not_called()
    assert nvim.api.buf_set_option.call_args_list == [
        call(42, "swapfile", False),
        call(42, "buflisted", True),
        call(42, "modifiable", False),
    ]


def test_apply_edit_locks_the_disk_loaded_buffer_when_the_original_vanished() -> None:
    state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=False)  # proven dedicated
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    tabnew_adds_a_tab(nvim)
    nvim.funcs.bufadd.return_value = 42
    ops = [EditOp(kind="replace", start_line=3, end_line=3, new_lines=("C",))]
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        result = follower.apply_edit("/tmp/f.py", ops)
    assert result == AnimationResult("completed", 1)
    goto_file.assert_not_called()
    assert nvim.api.buf_set_option.call_args_list == [
        call(42, "swapfile", False),
        call(42, "buflisted", True),
        call(42, "modifiable", False),
    ]


def test_goto_file_itself_never_touches_modifiable() -> None:
    # The lock lives in ensure_showing/_open_from_disk, never in goto_file:
    # show_fresh's callers unlock right after it anyway, and _drive owns
    # that lifecycle end to end.
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.exec_lua.return_value = 9
    tab, win = MagicMock(), MagicMock()
    buffer = MagicMock(number=9)
    nvim.api.list_tabpages.return_value = [tab]
    nvim.api.tabpage_list_wins.return_value = [win]
    nvim.api.win_get_buf.return_value = buffer
    with patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim):
        follower.goto_file("/tmp/f.py")
    nvim.api.buf_set_option.assert_not_called()


# Fail closed: a FollowerState that is gone (a concurrent `claude-follow stop`
# between two entry points of one hook) proves nothing about whose nvim this
# is, so no entry point locks a buffer and every event-firing command runs
# plainly, its messages left for the user to see (exec_logged adopted=True).


def _state_gone(tmp_path: Path) -> None:
    """A dedicated follower's state, written and then removed: what a
    `claude-follow stop` racing the hook leaves behind."""
    state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=False)
    state.FollowerState.clear("@1")
    assert state.FollowerState.read("@1") is None


def test_ensure_showing_with_the_state_gone_locks_nothing_and_logs_nothing(
    tmp_path: Path,
) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.exec_lua.side_effect = lambda code, *arguments: True if code == _IS_CLEAN_LUA else 9
    nvim.api.get_current_buf.return_value.handle = 9
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
        patch("vim_ai_follower.backends.nvim.exec_logged") as exec_logged,
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        _state_gone(tmp_path)
        follower.ensure_showing("/tmp/f.py")
    assert call(9, "modifiable", False) not in nvim.api.buf_set_option.call_args_list
    exec_logged.assert_called_once_with(nvim, "silent edit!", adopted=True)


def test_ensure_showing_of_a_new_file_with_the_state_gone_locks_nothing(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    tabnew_adds_a_tab(nvim)
    nvim.funcs.bufadd.return_value = 42
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.backends.nvim.exec_logged") as exec_logged,
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        _state_gone(tmp_path)
        follower.ensure_showing("/tmp/f.py")
    assert call(42, "modifiable", False) not in nvim.api.buf_set_option.call_args_list
    assert exec_logged.call_args_list == [
        call(nvim, "call bufload(42)", adopted=True),
        call(nvim, "filetype detect", adopted=True),
    ]


def test_reload_and_relock_with_the_state_gone_locks_nothing(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 9
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
        patch("vim_ai_follower.backends.nvim.exec_logged") as exec_logged,
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        _state_gone(tmp_path)
        follower.reload_and_relock("/tmp/f.py")
    nvim.api.buf_set_option.assert_not_called()
    exec_logged.assert_called_once_with(nvim, "edit!", adopted=True)


def test_a_completed_drive_with_the_state_gone_leaves_the_buffer_modifiable(
    tmp_path: Path,
) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    with patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path):
        _state_gone(tmp_path)
        result = follower._drive(nvim, 9, lambda: AnimationResult("completed", 1), ["x"])
    assert result == AnimationResult("completed", 1)
    assert nvim.api.buf_set_option.call_args_list == [call(9, "modifiable", True)]
