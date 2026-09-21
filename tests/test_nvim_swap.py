"""Unit coverage (mocked pynvim) for opening a file whose swap file is held
by a LIVE second editor. `_open_from_disk` turns the buffer's own swapfile
off between `bufadd` and `bufload`; the ordering is the whole point, so it is
asserted against `nvim.mock_calls` rather than per-attribute call lists.

The scoping half matters as much as the fix: the option is set on the one
buffer the follower just created, so nothing the follower never touched
changes behaviour. Tests here pin that negatively (the existing-buffer branch
and goto_file never mention swapfile); test_nvim_integration_swap.py proves it
positively against a real nvim."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

from vim_ai_follower import state
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends.nvim import NvimFollower
from vim_ai_follower.diff import EditOp


def _disk_loading_nvim() -> MagicMock:
    """A mock nvim where the file has no buffer yet, so both callers of
    _open_from_disk take the disk-reading branch and bufadd hands out 42."""
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = -1
    nvim.funcs.bufadd.return_value = 42
    return nvim


def _order(nvim: MagicMock, wanted: Any) -> int:
    return nvim.mock_calls.index(wanted)


def test_ensure_showing_disables_the_buffers_swapfile_before_loading_it() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = _disk_loading_nvim()
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
    ):
        follower.ensure_showing("/tmp/f.py")
    nvim.api.buf_set_option.assert_any_call(42, "swapfile", False)
    # Ordering is load-bearing: after bufload the swap check has already run
    # (and, measured, already created a second swap file), so a late setting
    # would be a no-op that still raised E325.
    assert _order(nvim, call.funcs.bufadd("/tmp/f.py")) < _order(
        nvim, call.api.buf_set_option(42, "swapfile", False)
    )
    assert _order(nvim, call.api.buf_set_option(42, "swapfile", False)) < _order(
        nvim, call.funcs.bufload(42)
    )


def test_apply_edits_vanished_buffer_branch_also_disables_the_swapfile() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = _disk_loading_nvim()
    ops = [EditOp(kind="replace", start_line=3, end_line=3, new_lines=("C",))]
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
    ):
        result = follower.apply_edit("/tmp/f.py", ops)
    assert result == AnimationResult("completed", 1)
    assert _order(nvim, call.api.buf_set_option(42, "swapfile", False)) < _order(
        nvim, call.funcs.bufload(42)
    )


def test_the_disk_loading_branch_writes_exactly_three_buffer_options() -> None:
    # Pins the whole option set, so an extra (or a GLOBAL) option write shows
    # up as a failure here rather than as a surprise in the user's own nvim.
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = _disk_loading_nvim()
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
    ):
        follower.ensure_showing("/tmp/f.py")
    assert nvim.api.buf_set_option.call_args_list == [
        call(42, "swapfile", False),
        call(42, "buflisted", True),
        call(42, "modifiable", False),
    ]
    nvim.api.set_option.assert_not_called()
    nvim.api.set_option_value.assert_not_called()
    assert [c.args[0] for c in nvim.command.call_args_list] == ["tabnew", "filetype detect"]


def test_an_adopted_follower_still_disables_the_swapfile_but_not_modifiable(
    tmp_path: Path,
) -> None:
    # The adopted guard governs the LOCK only. Refusing to open a file the
    # user's own editor holds would defeat the point of adopting it.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = _disk_loading_nvim()
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=True)
        follower.ensure_showing("/tmp/f.py")
    assert nvim.api.buf_set_option.call_args_list == [
        call(42, "swapfile", False),
        call(42, "buflisted", True),
    ]


def test_switching_to_an_existing_buffer_never_touches_its_swapfile() -> None:
    # Only the buffer _open_from_disk creates is opted out. A buffer that was
    # already there keeps whatever swap protection it had.
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = 9
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
    ):
        follower.ensure_showing("/tmp/f.py")
    assert "swapfile" not in [c.args[1] for c in nvim.api.buf_set_option.call_args_list]


def test_goto_file_never_touches_swapfile() -> None:
    # goto_file creates an EMPTY buffer and never reads disk, so no swap
    # check is in play and nothing should be opted out.
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = -1
    nvim.api.list_tabpages.return_value = []
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.goto_file("/tmp/f.py")
    nvim.api.buf_set_option.assert_not_called()
