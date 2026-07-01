from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock, patch

from vim_ai_follower.animate import (
    DEFAULT_PACE_SECONDS,
    LARGE_DIFF_LINE_THRESHOLD,
    KeySequence,
    apply,
    changed_line_count,
    pace_for,
    render_keystrokes,
)
from vim_ai_follower.diff import EditOp
from vim_ai_follower.tmux import TmuxPane


def test_delete_only_op_emits_ex_delete_command() -> None:
    ops = [EditOp(kind="delete", start_line=2, end_line=3, new_lines=())]
    assert render_keystrokes(ops) == [
        KeySequence(":2,3d", literal=True),
        KeySequence("Enter", literal=False),
    ]


def test_insert_at_top_uses_gg_open_above() -> None:
    ops = [EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a", "b"))]
    assert render_keystrokes(ops) == [
        KeySequence("gg", literal=True),
        KeySequence("O", literal=True),
        KeySequence("a", literal=True),
        KeySequence("Enter", literal=False),
        KeySequence("b", literal=True),
        KeySequence("Escape", literal=False),
    ]


def test_insert_mid_file_positions_then_opens_below() -> None:
    ops = [EditOp(kind="insert", start_line=3, end_line=2, new_lines=("c", "d"))]
    assert render_keystrokes(ops) == [
        KeySequence(":2", literal=True),
        KeySequence("Enter", literal=False),
        KeySequence("o", literal=True),
        KeySequence("c", literal=True),
        KeySequence("Enter", literal=False),
        KeySequence("d", literal=True),
        KeySequence("Escape", literal=False),
    ]


def test_replace_op_deletes_then_inserts() -> None:
    ops = [EditOp(kind="replace", start_line=2, end_line=2, new_lines=("X",))]
    assert render_keystrokes(ops) == [
        KeySequence(":2,2d", literal=True),
        KeySequence("Enter", literal=False),
        KeySequence(":1", literal=True),
        KeySequence("Enter", literal=False),
        KeySequence("o", literal=True),
        KeySequence("X", literal=True),
        KeySequence("Escape", literal=False),
    ]


def test_changed_line_count_sums_deletions_and_insertions() -> None:
    ops = [
        EditOp(kind="delete", start_line=2, end_line=3, new_lines=()),
        EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a", "b")),
    ]
    assert changed_line_count(ops) == 4


def test_pace_for_returns_default_below_threshold() -> None:
    ops = [EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))]
    assert pace_for(ops) == DEFAULT_PACE_SECONDS


def test_pace_for_uses_provided_base_pace_below_threshold() -> None:
    ops = [EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))]
    assert pace_for(ops, base_pace=0.15) == 0.15


def test_pace_for_returns_zero_above_threshold() -> None:
    big_lines = tuple(f"line{i}" for i in range(LARGE_DIFF_LINE_THRESHOLD + 1))
    ops = [EditOp(kind="insert", start_line=1, end_line=0, new_lines=big_lines)]
    assert pace_for(ops) == 0.0


def test_pace_for_ignores_base_pace_above_threshold() -> None:
    big_lines = tuple(f"line{i}" for i in range(LARGE_DIFF_LINE_THRESHOLD + 1))
    ops = [EditOp(kind="insert", start_line=1, end_line=0, new_lines=big_lines)]
    assert pace_for(ops, base_pace=0.15) == 0.0


def test_apply_sends_literal_and_named_keys_in_order() -> None:
    pane = cast(TmuxPane, MagicMock())
    sequences = [KeySequence(":2,3d", literal=True), KeySequence("Enter", literal=False)]
    apply(pane, sequences, pace_seconds=0.0)
    pane.send_text.assert_called_once_with(":2,3d")  # type: ignore[attr-defined]
    pane.send_key.assert_called_once_with("Enter")  # type: ignore[attr-defined]


def test_apply_sleeps_between_each_sequence() -> None:
    pane = cast(TmuxPane, MagicMock())
    sequences = [KeySequence("a", literal=True), KeySequence("b", literal=True)]
    with patch("vim_ai_follower.animate.time.sleep") as sleep:
        apply(pane, sequences, pace_seconds=0.05)
    assert sleep.call_count == 2
    sleep.assert_called_with(0.05)
