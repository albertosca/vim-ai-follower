"""Unit coverage (mocked pynvim) for _run_ops' interrupt rollback: an
interrupt inside op k must put the op's deleted range back and remove the
rows _animate_lines typed, so the buffer equals apply_ops(before, ops[:k])
— the state the interrupt notification quotes to Claude. Asserts the exact
buf_get_lines/buf_set_lines traffic. Kept in its own file per the backlog
contract (other agents edit test_nvim.py in parallel)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends.nvim import NvimFollower
from vim_ai_follower.diff import EditOp


@pytest.fixture(autouse=True)
def _no_sleep() -> Iterator[None]:
    with patch("vim_ai_follower.backends.nvim.time.sleep"):
        yield


def _mock_nvim(line_counts: list[int], old_lines: list[str]) -> MagicMock:
    """A pynvim double whose buf_line_count answers a scripted sequence (the
    real buffer shrinks and grows as the op deletes and types) and whose
    buf_get_lines returns the range the op is about to delete."""
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.api.buf_line_count.side_effect = line_counts
    nvim.api.buf_get_lines.return_value = old_lines
    return nvim


# _run_ops reads the whole buffer once per run (the base of the persisted
# crash-fallback partial); the rollback snapshot is every OTHER read.
_INITIAL_READ = call(7, 0, -1, True)


def _snapshots(nvim: MagicMock) -> list[Any]:
    return [c for c in nvim.api.buf_get_lines.call_args_list if c != _INITIAL_READ]


def test_interrupt_mid_line_restores_the_deleted_range_and_drops_the_typed_row(
    tmp_path: Path,
) -> None:
    # Buffer ["a", "b", "c"]; op replaces line 2 with "WX". Interrupt after
    # the "W": the row _animate_lines inserted holds a half-typed line and
    # "b" is gone — both must be undone by one buf_set_lines over rows 1..2.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.05)
    # start_line 2 short-circuits the wipes_buffer check, so buf_line_count is
    # asked exactly twice: after the delete (2 = ["a", "c"]) and on the
    # interrupt (3 = ["a", "", "c"] with "W" typed into the middle row).
    nvim = _mock_nvim([2, 3], ["b"])
    op = EditOp(kind="replace", start_line=2, end_line=2, new_lines=("WX",))
    # op boundary + line0 boundary + char0("W") -> 3 calls; the 4th (char1,
    # before "X") interrupts.
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=[None, None, None, "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("interrupted", 0)
    assert _snapshots(nvim) == [call(7, 1, 2, True)]
    assert nvim.api.buf_set_lines.call_args_list == [
        call(7, 1, 2, True, []),  # the op's delete
        call(7, 1, 1, True, [""]),  # _animate_lines' row for "WX"
        call(7, 1, 2, True, ["b"]),  # rollback: one typed row out, "b" back
    ]


def test_interrupt_at_the_first_char_of_the_first_new_line_still_rolls_back(
    tmp_path: Path,
) -> None:
    # Nothing was typed yet, but the row was already inserted (blank) and the
    # delete already happened — the rollback must still fire.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.05)
    nvim = _mock_nvim([2, 3], ["b"])
    op = EditOp(kind="replace", start_line=2, end_line=2, new_lines=("WX",))
    # op boundary + line0 boundary -> 2 calls; the 3rd (char0) interrupts.
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=[None, None, "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("interrupted", 0)
    nvim.api.buf_set_text.assert_not_called()
    assert nvim.api.buf_set_lines.call_args_list[-1] == call(7, 1, 2, True, ["b"])


def test_interrupt_at_a_new_line_boundary_rolls_back_the_full_typed_region(
    tmp_path: Path,
) -> None:
    # Two new lines, the first fully typed, interrupted at the second's
    # boundary before its row exists: the region to replace is one row wide.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.05)
    nvim = _mock_nvim([2, 3], ["b"])
    op = EditOp(kind="replace", start_line=2, end_line=2, new_lines=("W", "Z"))
    # op boundary + line0 boundary + char0("W") -> 3; the 4th (line1
    # boundary) interrupts.
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=[None, None, None, "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("interrupted", 0)
    assert nvim.api.buf_set_lines.call_args_list[-1] == call(7, 1, 2, True, ["b"])


def test_interrupt_inside_a_wiping_op_swallows_the_implicit_blank(tmp_path: Path) -> None:
    # The op empties the buffer, so nvim keeps an implicit blank line that
    # _animate_lines pushes below every typed row. The rollback region must
    # include it, or the restored buffer would end with a spurious blank.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.05)
    # buf_line_count: 3 (wipes_buffer test, pre-delete), 1 (post-delete: only
    # the implicit blank), 2 (interrupt: one typed row + the implicit blank).
    nvim = _mock_nvim([3, 1, 2], ["a", "b", "c"])
    op = EditOp(kind="replace", start_line=1, end_line=3, new_lines=("XY", "Z"))
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=[None, None, None, "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("interrupted", 0)
    assert _snapshots(nvim) == [call(7, 0, 3, True)]
    assert nvim.api.buf_set_lines.call_args_list == [
        call(7, 0, 3, True, []),  # the op's delete (buffer -> implicit blank)
        call(7, 0, 0, True, [""]),  # _animate_lines' row for "XY"
        # rows 0..2 = the typed row AND the implicit blank, both replaced by
        # the original three lines: no trailing blank survives.
        call(7, 0, 2, True, ["a", "b", "c"]),
    ]


def test_an_op_boundary_interrupt_leaves_the_buffer_untouched(tmp_path: Path) -> None:
    # Control: the signal check at the top of the op loop runs BEFORE the
    # delete, so there is nothing to roll back — and nothing to snapshot.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.05)
    nvim = _mock_nvim([], ["b"])
    op = EditOp(kind="replace", start_line=2, end_line=2, new_lines=("WX",))
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=["interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("interrupted", 0)
    assert _snapshots(nvim) == []
    nvim.api.buf_set_lines.assert_not_called()


def test_a_delete_only_op_is_neither_snapshotted_nor_rolled_back(tmp_path: Path) -> None:
    # A delete-only op never calls _animate_lines, so no signal check runs
    # between its delete and its index bump: it cannot stop mid-op. The
    # interrupt lands at the NEXT op's boundary and op 0 stays applied.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.05)
    nvim = _mock_nvim([], [])
    ops = [
        EditOp(kind="delete", start_line=3, end_line=3, new_lines=()),
        EditOp(kind="delete", start_line=1, end_line=1, new_lines=()),
    ]
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", ops)
    assert result == AnimationResult("interrupted", 1)  # op 0 genuinely done
    assert _snapshots(nvim) == []
    assert nvim.api.buf_set_lines.call_args_list == [call(7, 2, 3, True, [])]


def test_a_completed_op_never_rolls_back(tmp_path: Path) -> None:
    # The snapshot is taken on every animating op, but it must only ever be
    # written back on the interrupted path.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = _mock_nvim([2], ["b"])
    op = EditOp(kind="replace", start_line=2, end_line=2, new_lines=("WX",))
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("completed", 1)
    assert _snapshots(nvim) == [call(7, 1, 2, True)]  # snapshot taken
    assert call(7, 1, 2, True, ["b"]) not in nvim.api.buf_set_lines.call_args_list
