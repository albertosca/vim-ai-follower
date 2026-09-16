"""Regression tests for the trailing-blank-line bug: an apply_edit op whose
deletion empties the buffer down to nvim's implicit single blank line must
not leave that blank line stranded at the bottom after the new content is
typed in. See NvimFollower._run_ops for the mechanism."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends.nvim import NvimFollower
from vim_ai_follower.diff import EditOp, compute_edit_script


def test_apply_edit_drops_the_implicit_blank_when_an_op_wipes_the_whole_buffer(
    tmp_path: Path,
) -> None:
    # A single-line buffer ("a = 1") replaced entirely: the delete leaves
    # nvim's implicit blank line, which _animate_lines' insert-before-row
    # mechanism pushes past every newly typed line. It must be dropped once
    # typing completes, exactly as show_fresh drops its own seed blank.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.api.buf_line_count.return_value = 1
    ops = compute_edit_script("a = 1\n", "a = 2\n")
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", ops)
    assert result == AnimationResult("completed", 1)
    # the op deletes the sole line, then the pushed-down implicit blank
    # (now sitting right after the one typed line) gets dropped
    assert nvim.api.buf_set_lines.call_args_list[-1] == call(7, 1, 2, True, [])


def test_apply_edit_wipe_and_replace_of_a_multiline_buffer_drops_only_one_blank(
    tmp_path: Path,
) -> None:
    # A 3-line buffer entirely replaced by 2 new lines: same mechanism, just
    # not a single-line file, to prove the fix isn't special-cased to N=1.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.api.buf_line_count.return_value = 3
    ops = compute_edit_script("a\nb\nc\n", "x\ny\n")
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", ops)
    assert result == AnimationResult("completed", len(ops))
    # 2 new lines typed at rows 0/1; the implicit blank pushed to row 2 is
    # dropped exactly once (start_row 0 + len(new_lines) 2 = drop at 2..3).
    assert call(7, 2, 3, True, []) in nvim.api.buf_set_lines.call_args_list


def test_apply_edit_does_not_drop_a_line_when_other_content_survives(tmp_path: Path) -> None:
    # Control case: the op's deletion does NOT empty the whole buffer (line 3
    # survives above/below), so nvim never falls back to an implicit blank —
    # no extra buf_set_lines drop call should be issued.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.api.buf_line_count.return_value = 3
    ops = compute_edit_script("a\nb\nc\n", "a\nB\nc\n")
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", ops)
    assert result == AnimationResult("completed", len(ops))
    # the op deletes the old line-2 range, then _animate_lines' own
    # insert-before-row seed for the one new line — and nothing else: no
    # extra drop call trims anything away, because the deletion left line 3
    # (and line 1) genuinely in the buffer, not an implicit blank.
    assert nvim.api.buf_set_lines.call_args_list == [
        call(7, 1, 2, True, []),
        call(7, 1, 1, True, [""]),
    ]


def test_apply_edit_pure_delete_that_empties_the_buffer_leaves_the_implicit_blank(
    tmp_path: Path,
) -> None:
    # A delete-only op (no new_lines) that empties the whole buffer must NOT
    # trigger a drop: nvim's implicit blank line IS the correct, final
    # representation of an emptied buffer here (same as an empty file).
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.api.buf_line_count.return_value = 1
    op = EditOp(kind="delete", start_line=1, end_line=1, new_lines=())
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("completed", 1)
    nvim.api.buf_set_lines.assert_called_once_with(7, 0, 1, True, [])


def test_apply_edit_interrupted_mid_wipe_does_not_drop_anything(tmp_path: Path) -> None:
    # An interrupt inside the op's animation returns immediately: whatever
    # partial state the buffer is in is superseded by the higher-level
    # rewrite_buffer + resume replay (hooks.py), so _run_ops must not try to
    # clean up the implicit blank itself on this path.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.api.buf_line_count.return_value = 1
    op = EditOp(kind="replace", start_line=1, end_line=1, new_lines=("a", "b"))
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        # op boundary (None), line0 (None, typed), line1 boundary -> interrupt.
        patch("vim_ai_follower.control.check_signal", side_effect=[None, None, "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("interrupted", 0)
    # only the op's own delete and the one inserted-then-typed line — no
    # trailing drop call was issued on the interrupted path.
    assert nvim.api.buf_set_lines.call_args_list == [
        call(7, 0, 1, True, []),
        call(7, 0, 0, True, [""]),
    ]
