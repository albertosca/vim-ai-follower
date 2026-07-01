from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock, patch

from vim_ai_follower.animate import (
    KeySequence,
    apply,
    render_full_type,
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


def test_render_full_type_empty_lines_returns_nothing() -> None:
    assert render_full_type(()) == []


def test_render_full_type_enters_insert_mode_and_types_each_line() -> None:
    assert render_full_type(("a", "b", "c")) == [
        KeySequence("i", literal=True),
        KeySequence("a", literal=True),
        KeySequence("Enter", literal=False),
        KeySequence("b", literal=True),
        KeySequence("Enter", literal=False),
        KeySequence("c", literal=True),
        KeySequence("Escape", literal=False),
    ]


def test_render_full_type_single_line() -> None:
    assert render_full_type(("only",)) == [
        KeySequence("i", literal=True),
        KeySequence("only", literal=True),
        KeySequence("Escape", literal=False),
    ]


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


def test_apply_stops_sleeping_once_the_deadline_passes() -> None:
    pane = cast(TmuxPane, MagicMock())
    sequences = [KeySequence("a"), KeySequence("b"), KeySequence("c")]
    with (
        patch("vim_ai_follower.animate.time.sleep") as sleep,
        patch("vim_ai_follower.animate.time.monotonic", side_effect=[0.0, 0.0, 5.0, 10.0]),
    ):
        apply(pane, sequences, pace_seconds=1.0, max_seconds=4.0)
    assert sleep.call_count == 1
    assert pane.send_text.call_count == 3  # type: ignore[attr-defined]


def test_apply_with_zero_pace_never_checks_the_clock() -> None:
    pane = cast(TmuxPane, MagicMock())
    sequences = [KeySequence("a"), KeySequence("b")]
    with (
        patch("vim_ai_follower.animate.time.sleep") as sleep,
        patch("vim_ai_follower.animate.time.monotonic", side_effect=[0.0]) as monotonic,
    ):
        apply(pane, sequences, pace_seconds=0.0)
    sleep.assert_not_called()
    monotonic.assert_called_once()  # only the initial deadline computation
