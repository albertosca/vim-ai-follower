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

import pytest

from vim_ai_follower import state
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends.nvim import NvimFollower
from vim_ai_follower.diff import EditOp


def _disk_loading_nvim() -> MagicMock:
    """A mock nvim where the file has no buffer yet, so both callers of
    _open_from_disk take the disk-reading branch and bufadd hands out 42."""
    nvim = MagicMock()
    nvim.exec_lua.return_value = -1
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
        nvim, call.api.exec2("call bufload(42)", {"output": True})
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
        nvim, call.api.exec2("call bufload(42)", {"output": True})
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
    # The event-firing commands go through exec2 (plugin output captured and
    # logged, see nvim_prompt.exec_logged); only tabnew stays a plain command.
    assert [c.args[0] for c in nvim.command.call_args_list] == ["tabnew"]
    assert [c.args[0] for c in nvim.api.exec2.call_args_list] == [
        "call bufload(42)",
        "filetype detect",
    ]


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
    nvim.exec_lua.return_value = 9
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
    ):
        follower.ensure_showing("/tmp/f.py")
    assert "swapfile" not in [c.args[1] for c in nvim.api.buf_set_option.call_args_list]


def test_goto_file_opts_the_buffer_it_creates_out_of_swap_before_naming_it() -> None:
    # goto_file creates an EMPTY buffer and never reads disk, but NAMING it
    # runs the swap check: with a live editor holding the file's swap,
    # buf_set_name raised E325 (measured 2026-09-28,
    # tests/test_nvim_integration_swap.py). An earlier version of this test
    # pinned "never touches swapfile" on the premise that no check was in play.
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.exec_lua.return_value = -1
    nvim.api.list_tabpages.return_value = []
    buf = nvim.api.create_buf.return_value
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.goto_file("/tmp/f.py")
    assert nvim.api.buf_set_option.call_args_list == [call(buf, "swapfile", False)]
    assert nvim.api.buf_set_name.call_args_list == [call(buf, "/tmp/f.py")]
    assert nvim.api.mock_calls.index(call.buf_set_option(buf, "swapfile", False)) < (
        nvim.api.mock_calls.index(call.buf_set_name(buf, "/tmp/f.py"))
    )


def test_show_fresh_opts_its_new_buffer_out_of_swap_before_naming_it(tmp_path: Path) -> None:
    # The retype names a fresh buffer after the file; with a live editor
    # holding the file's swap, naming a swap-enabled buffer raised E325 and
    # left the UI at a blocking hit-enter prompt (measured 2026-09-28).
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.exec_lua.return_value = -1
    buf = nvim.current.buffer
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        follower.show_fresh("/tmp/f.py", "a\n")
    calls = nvim.api.mock_calls
    assert calls.index(call.buf_set_option(buf, "swapfile", False)) < calls.index(
        call.buf_set_name(buf, "/tmp/f.py")
    )


def _goto_hidden_buffer(loaded: bool) -> MagicMock:
    """goto_file on a buffer that exists (number 5) but has no window."""
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.exec_lua.return_value = 5
    nvim.api.list_tabpages.return_value = []
    nvim.api.buf_is_loaded.return_value = loaded
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.goto_file("/tmp/f.py")
    return nvim


def test_goto_file_opts_an_unloaded_buffer_out_of_swap_before_loading_it() -> None:
    # win_set_buf LOADS an unloaded buffer, and the load runs the swap check:
    # E325 with a live editor holding the swap (measured 2026-09-28).
    nvim = _goto_hidden_buffer(loaded=False)
    calls = nvim.api.mock_calls
    assert calls.index(call.buf_set_option(5, "swapfile", False)) < calls.index(
        call.win_set_buf(0, 5)
    )


def test_goto_file_leaves_a_loaded_buffers_swap_alone() -> None:
    nvim = _goto_hidden_buffer(loaded=True)
    assert "swapfile" not in [c.args[1] for c in nvim.api.buf_set_option.call_args_list]
    assert call.win_set_buf(0, 5) in nvim.api.mock_calls


_SWAP_BACK_ON = (
    # Spelled out, not imported: an import would agree with any change.
    "let g:vaf_shortmess = &shortmess | set shortmess+=A"
    " | try | setlocal swapfile"
    " | finally | let &shortmess = g:vaf_shortmess | unlet g:vaf_shortmess | endtry"
)


@pytest.mark.parametrize("adopted", [True, False], ids=["adopted", "dedicated"])
def test_show_fresh_turns_swap_back_on_after_naming_only_when_adopted(
    adopted: bool, tmp_path: Path
) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.exec_lua.return_value = -1
    buf = nvim.current.buffer
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=adopted)
        follower.show_fresh("/tmp/f.py", "a\n")
    calls = nvim.api.mock_calls
    back_on = call.exec2(_SWAP_BACK_ON, {"output": True})
    if adopted:
        assert calls.index(call.buf_set_name(buf, "/tmp/f.py")) < calls.index(back_on)
    else:
        assert back_on not in calls


@pytest.mark.parametrize("adopted", [True, False], ids=["adopted", "dedicated"])
@pytest.mark.parametrize("existing", [-1, 5], ids=["created", "unloaded"])
def test_goto_file_turns_swap_back_on_after_loading_only_when_adopted(
    adopted: bool, existing: int, tmp_path: Path
) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=adopted)
    nvim = MagicMock()
    nvim.exec_lua.return_value = existing
    nvim.api.list_tabpages.return_value = []
    nvim.api.buf_is_loaded.return_value = False
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.goto_file("/tmp/f.py")
    calls = nvim.api.mock_calls
    back_on = call.exec2(_SWAP_BACK_ON, {"output": True})
    shown = [c for c in calls if c[0] == "win_set_buf"]
    assert shown
    if adopted:
        assert calls.index(shown[-1]) < calls.index(back_on)
    else:
        assert back_on not in calls


def test_goto_file_leaves_a_loaded_buffers_swap_alone_even_when_adopted() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=True)
    nvim = MagicMock()
    nvim.exec_lua.return_value = 5
    nvim.api.list_tabpages.return_value = []
    nvim.api.buf_is_loaded.return_value = True
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.goto_file("/tmp/f.py")
    assert call.exec2(_SWAP_BACK_ON, {"output": True}) not in nvim.api.mock_calls


@pytest.mark.parametrize("adopted", [True, False], ids=["adopted", "dedicated"])
def test_open_from_disk_turns_swap_back_on_after_loading_only_when_adopted(
    adopted: bool,
) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=adopted)
    nvim = MagicMock()
    nvim.exec_lua.return_value = -1
    nvim.funcs.bufadd.return_value = 42
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.ensure_showing("/tmp/f.py")
    calls = nvim.api.mock_calls
    back_on = call.exec2(_SWAP_BACK_ON, {"output": True})
    if adopted:
        assert calls.index(call.win_set_buf(0, 42)) < calls.index(back_on)
    else:
        assert back_on not in calls
