"""Unit coverage (mocked pynvim) for the mid-line interrupt behavior change:
on interrupt, _animate_lines must leave the current line exactly as far as it
was typed (no snap to its full text) and must not count that line as shown.
Pattern mirrors tests/test_nvim.py; kept in its own file per the backlog
contract (other agents edit test_nvim.py in parallel)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends.nvim import NvimFollower, _animate_lines
from vim_ai_follower.diff import EditOp


@pytest.fixture(autouse=True)
def _no_sleep() -> Iterator[None]:
    with patch("vim_ai_follower.backends.nvim.time.sleep"):
        yield


def test_interrupt_mid_char_leaves_only_the_typed_prefix_no_snap() -> None:
    # line0 "hi" fully typed, line1 "abcd" interrupted after 2 of its 4 chars.
    signals = [None, None, None, None, None, None, "interrupt"]
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", side_effect=signals):
        result = _animate_lines(nvim, 7, ("hi", "abcd"), 0, lambda: 0.05, "@1", ns=1)
    assert result == AnimationResult("interrupted", 1)  # line1 not counted as shown
    assert nvim.api.buf_set_text.call_args_list == [
        call(7, 0, 0, 0, 0, ["h"]),
        call(7, 0, 1, 0, 1, ["i"]),
        call(7, 1, 0, 1, 0, ["a"]),
        call(7, 1, 1, 1, 1, ["b"]),
    ]
    # no snap call for the untyped "cd" remainder
    assert call(7, 1, 2, 1, 2, ["cd"]) not in nvim.api.buf_set_text.call_args_list


def test_interrupt_at_the_very_first_char_of_a_line_leaves_it_untouched() -> None:
    # The interrupted line's row was already inserted (blank) by the outer
    # loop before the per-char check ever ran, but zero characters land in
    # it — the row stays empty, and the line is still not counted.
    signals = [None, "interrupt"]
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", side_effect=signals):
        result = _animate_lines(nvim, 7, ("xyz",), 0, lambda: 0.05, "@1", ns=1)
    assert result == AnimationResult("interrupted", 0)
    nvim.api.buf_set_text.assert_not_called()  # no snap of "xyz"


def test_interrupt_after_several_full_lines_then_mid_next_line_counts_only_the_full_ones() -> None:
    # Three full lines ("a", "b", "c"), then interrupted 1 char into "defg".
    signals = [
        None,  # outer line0
        None,  # char0 of "a"
        None,  # outer line1
        None,  # char0 of "b"
        None,  # outer line2
        None,  # char0 of "c"
        None,  # outer line3
        None,  # char0 of "defg" ('d')
        "interrupt",  # char1 of "defg" (before 'e')
    ]
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", side_effect=signals):
        result = _animate_lines(nvim, 7, ("a", "b", "c", "defg"), 0, lambda: 0.05, "@1", ns=1)
    assert result == AnimationResult("interrupted", 3)  # only a, b, c counted
    assert call(7, 3, 0, 3, 0, ["d"]) in nvim.api.buf_set_text.call_args_list
    assert call(7, 3, 1, 3, 1, ["efg"]) not in nvim.api.buf_set_text.call_args_list  # no snap


def test_show_fresh_interrupted_mid_line_buffer_call_holds_only_the_typed_prefix(
    tmp_path: Path,
) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.05)
    nvim = MagicMock()
    nvim.current.buffer.handle = 7
    # outer(line0="ab") + char0 + char1 [types "ab"] = 3 calls; outer(line1) +
    # char0("w") -> 2 more calls; the 6th call (char1, before 'x') interrupts.
    signals = [None, None, None, None, None, "interrupt"]
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=signals),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.show_fresh("/tmp/f.py", "ab\nwxyz\n")
    assert result == AnimationResult("interrupted", 1)
    assert nvim.api.buf_set_text.call_args_list == [
        call(7, 0, 0, 0, 0, ["a"]),
        call(7, 0, 1, 0, 1, ["b"]),
        call(7, 1, 0, 1, 0, ["w"]),
    ]
    # left modifiable and unlocked — the user owns the buffer after an interrupt
    assert nvim.api.buf_set_option.call_args_list[0] == call(7, "modifiable", True)
    assert call(7, "modifiable", False) not in nvim.api.buf_set_option.call_args_list


def test_apply_edit_interrupted_mid_op_line_reports_op_index_with_no_snap(
    tmp_path: Path,
) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.05)
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.api.buf_line_count.return_value = 1
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("wxyz",))
    # op boundary + outer line-boundary + char0("w") -> 3 calls; the 4th call
    # (char1, before 'x') interrupts.
    signals = [None, None, None, "interrupt"]
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=signals),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("interrupted", 0)  # op-level bookkeeping unaffected
    assert nvim.api.buf_set_text.call_args_list == [call(7, 0, 0, 0, 0, ["w"])]
