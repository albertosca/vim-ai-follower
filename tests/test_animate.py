from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

from vim_ai_follower import control
from vim_ai_follower.animate import (
    AnimationResult,
    ApplyResult,
    KeySequence,
    _delete_sequences,
    _insert_sequences,
    _line_sequences,
    apply,
    render_keystrokes,
    run_lines,
    run_ops,
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


def test_line_sequences_first_line_opens_with_i() -> None:
    sequences, threshold = _line_sequences("alpha", "i")
    assert sequences == [
        KeySequence("i", literal=True),
        KeySequence("alpha", literal=True),
        KeySequence("Escape", literal=False),
    ]
    assert threshold == 2  # only opener+text creates an undoable change


def test_line_sequences_later_line_opens_with_o() -> None:
    sequences, threshold = _line_sequences("beta", "o")
    assert sequences == [
        KeySequence("o", literal=True),
        KeySequence("beta", literal=True),
        KeySequence("Escape", literal=False),
    ]
    assert threshold == 1  # 'o' alone already opened a line


def test_line_sequences_empty_first_line_never_undoes() -> None:
    sequences, threshold = _line_sequences("", "i")
    assert threshold == len(sequences) + 1  # 'i' + empty text: no change ever


def test_apply_sends_literal_and_named_keys_and_reports_completed() -> None:
    pane = cast(TmuxPane, MagicMock())
    sequences = [KeySequence(":2,3d", literal=True), KeySequence("Enter", literal=False)]
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        result = apply(pane, sequences, pace_seconds=0.0, session_id="$1")
    pane.send_text.assert_called_once_with(":2,3d")  # type: ignore[attr-defined]
    pane.send_key.assert_called_once_with("Enter")  # type: ignore[attr-defined]
    assert result == ApplyResult("completed", 2)


def test_apply_sleeps_between_each_sequence() -> None:
    pane = cast(TmuxPane, MagicMock())
    sequences = [KeySequence("a", literal=True), KeySequence("b", literal=True)]
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.animate.time.sleep") as sleep,
    ):
        result = apply(pane, sequences, pace_seconds=0.05, session_id="$1")
    assert sleep.call_count == 2
    sleep.assert_called_with(0.05)
    assert result == ApplyResult("completed", 2)


def test_apply_stops_sleeping_once_the_deadline_passes() -> None:
    pane = cast(TmuxPane, MagicMock())
    sequences = [KeySequence("a"), KeySequence("b"), KeySequence("c")]
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.animate.time.sleep") as sleep,
        patch("vim_ai_follower.animate.time.monotonic", side_effect=[0.0, 0.0, 5.0, 10.0]),
    ):
        result = apply(pane, sequences, pace_seconds=1.0, session_id="$1", max_seconds=4.0)
    assert sleep.call_count == 1
    assert pane.send_text.call_count == 3  # type: ignore[attr-defined]
    assert result == ApplyResult("completed", 3)


def test_apply_with_zero_pace_never_checks_the_clock() -> None:
    pane = cast(TmuxPane, MagicMock())
    sequences = [KeySequence("a"), KeySequence("b")]
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.animate.time.sleep") as sleep,
        patch("vim_ai_follower.animate.time.monotonic", side_effect=[0.0]) as monotonic,
    ):
        result = apply(pane, sequences, pace_seconds=0.0, session_id="$1")
    sleep.assert_not_called()
    monotonic.assert_called_once()  # only the initial deadline computation
    assert result == ApplyResult("completed", 2)


def test_apply_stops_before_sending_when_interrupted_immediately() -> None:
    pane = cast(TmuxPane, MagicMock())
    sequences = [KeySequence("a"), KeySequence("b")]
    with patch("vim_ai_follower.control.check_signal", return_value="interrupt"):
        result = apply(pane, sequences, pace_seconds=0.0, session_id="$1")
    pane.send_text.assert_not_called()  # type: ignore[attr-defined]
    assert result == ApplyResult("interrupted", 0)


def test_apply_stops_partway_when_paused_mid_sequence() -> None:
    pane = cast(TmuxPane, MagicMock())
    sequences = [KeySequence("a"), KeySequence("b"), KeySequence("c")]
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, None, "pause"]):
        result = apply(pane, sequences, pace_seconds=0.0, session_id="$1")
    assert pane.send_text.call_count == 2  # type: ignore[attr-defined]
    assert result == ApplyResult("paused", 2)


def test_apply_passes_session_id_and_base_dir_to_check_signal() -> None:
    pane = cast(TmuxPane, MagicMock())
    with patch("vim_ai_follower.control.check_signal", return_value=None) as check:
        apply(
            pane,
            [KeySequence("a")],
            pace_seconds=0.0,
            session_id="$7",
            control_base_dir=Path("/tmp/x"),
        )
    check.assert_called_once_with("$7", Path("/tmp/x"))


def test_delete_sequences_for_delete_op() -> None:
    op = EditOp(kind="delete", start_line=2, end_line=3, new_lines=())
    assert _delete_sequences(op) == [
        KeySequence(":2,3d", literal=True),
        KeySequence("Enter", literal=False),
    ]


def test_delete_sequences_empty_for_pure_insert_op() -> None:
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    assert _delete_sequences(op) == []


def test_insert_sequences_empty_for_pure_delete_op() -> None:
    op = EditOp(kind="delete", start_line=2, end_line=3, new_lines=())
    assert _insert_sequences(op) == ([], 0)


def test_insert_sequences_at_top_uses_gg_open_above() -> None:
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a", "b"))
    sequences, prefix_len = _insert_sequences(op)
    assert sequences == [
        KeySequence("gg", literal=True),
        KeySequence("O", literal=True),
        KeySequence("a", literal=True),
        KeySequence("Enter", literal=False),
        KeySequence("b", literal=True),
        KeySequence("Escape", literal=False),
    ]
    assert prefix_len == 2  # "gg", "O"


def test_insert_sequences_mid_file_positions_then_opens_below() -> None:
    op = EditOp(kind="insert", start_line=3, end_line=2, new_lines=("c", "d"))
    sequences, prefix_len = _insert_sequences(op)
    assert sequences == [
        KeySequence(":2", literal=True),
        KeySequence("Enter", literal=False),
        KeySequence("o", literal=True),
        KeySequence("c", literal=True),
        KeySequence("Enter", literal=False),
        KeySequence("d", literal=True),
        KeySequence("Escape", literal=False),
    ]
    # the prefix is derived from the same list it describes — it can't desync
    assert prefix_len == 3
    assert [s.text for s in sequences[:prefix_len]] == [":2", "Enter", "o"]


def test_run_ops_clears_stale_signals_before_starting(tmp_path: Path) -> None:
    control.request_pause("$1", base_dir=tmp_path)
    pane = cast(TmuxPane, MagicMock())
    ops = [EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))]
    result = run_ops(pane, "$1", ops, pace_seconds=0.0, base_dir=tmp_path)
    # the stale pause from before this call started must not affect it
    assert result == AnimationResult("completed", 1)


def test_run_ops_all_complete() -> None:
    pane = cast(TmuxPane, MagicMock())
    ops = [
        EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",)),
        EditOp(kind="replace", start_line=3, end_line=3, new_lines=("b",)),
    ]
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_ops(pane, "$1", ops, pace_seconds=0.0)
    assert result == AnimationResult("completed", 2)


def test_run_ops_all_complete_with_a_pure_delete_op_followed_by_more_ops() -> None:
    # A pure-delete op has no insert half at all — this exercises the branch
    # where run_ops loops back to the next op without ever entering the
    # `if insert_seq:` block, as opposed to every other run_ops test, where
    # each op interrupted or completed has a non-empty insert half.
    pane = cast(TmuxPane, MagicMock())
    ops = [
        EditOp(kind="delete", start_line=2, end_line=3, new_lines=()),
        EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",)),
    ]
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_ops(pane, "$1", ops, pace_seconds=0.0)
    assert result == AnimationResult("completed", 2)


def test_run_ops_interrupted_before_first_op_undoes_nothing() -> None:
    pane = cast(TmuxPane, MagicMock())
    ops = [EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))]
    with (
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_ops(pane, "$1", ops, pace_seconds=0.0)
    assert result == AnimationResult("interrupted", 0)
    pane.send_text.assert_not_called()  # type: ignore[attr-defined]
    pane.send_key.assert_called_once_with("Escape")  # type: ignore[attr-defined]


def test_run_ops_interrupted_mid_insert_undoes_the_insert(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    # pure-insert op, anchor<1 so the sequence is ["gg", "O", "a", "Enter",
    # "b", "Escape"] (prefix length 2). Two Nones let "gg" and "O" through —
    # insert mode is now entered — then "interrupt" fires before "a" is sent.
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a", "b"))
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, None, "interrupt"]):
        result = run_ops(pane, "$1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 0)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts == ["gg", "O", "u"]
    pane.send_key.assert_called_once_with("Escape")  # type: ignore[attr-defined]


def test_run_ops_interrupted_before_insert_mode_entered_skips_undo(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a", "b"))
    # signal fires on the very first check inside the insert-half apply()
    # call — before "gg" is even sent, so insert mode was never entered
    with patch("vim_ai_follower.control.check_signal", side_effect=["pause"]):
        result = run_ops(pane, "$1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("paused", 0)
    pane.send_key.assert_called_once_with("Escape")  # type: ignore[attr-defined]
    pane.send_text.assert_not_called()  # type: ignore[attr-defined]


def test_run_ops_interrupted_during_delete_needs_no_undo(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="delete", start_line=2, end_line=3, new_lines=())
    # the command text (":2,3d") gets typed, but the signal fires before its
    # Enter — the ex-command was never executed, so Escape alone is enough
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, "interrupt"]):
        result = run_ops(pane, "$1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 0)
    pane.send_text.assert_called_once_with(":2,3d")  # type: ignore[attr-defined]
    pane.send_key.assert_called_once_with("Escape")  # type: ignore[attr-defined]


def test_run_ops_pause_in_insert_half_undoes_both_halves_of_a_replace_op(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="replace", start_line=2, end_line=3, new_lines=("X", "Y"))
    # delete half (":2,3d", Enter) passes 2 checks; the insert half's prefix
    # (":1", Enter, "o") passes 3 more; pause fires before the text "X".
    # The insert change AND the already-executed delete must both be undone:
    # a single undo leaves the delete applied, and replaying the saved op
    # would re-run ":2,3d" against lines that have shifted — destroying them.
    with patch(
        "vim_ai_follower.control.check_signal",
        side_effect=[None, None, None, None, None, "pause"],
    ):
        result = run_ops(pane, "$1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("paused", 0)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts.count("u") == 2  # insert undone AND delete undone
    pending = control.load_pending_animation("$1", base_dir=tmp_path)
    assert pending == control.PendingApplyEdit([op], 0.0)  # full op valid to replay


def test_run_ops_pause_before_insert_prefix_still_undoes_the_delete(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="replace", start_line=2, end_line=3, new_lines=("X",))
    # pause at the insert half's FIRST check: nothing of the insert was sent,
    # but the delete already ran and must be rolled back
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, None, "pause"]):
        result = run_ops(pane, "$1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("paused", 0)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts.count("u") == 1  # delete rollback only


def test_run_ops_interrupt_in_insert_half_of_replace_restores_the_op_boundary(
    tmp_path: Path,
) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="replace", start_line=2, end_line=3, new_lines=("X",))
    # interrupt after the insert prefix completed: undo insert, undo delete —
    # the buffer must equal apply_ops(before, ops[:0]) so the notification
    # sent to Claude reflects what is actually on screen
    with patch(
        "vim_ai_follower.control.check_signal",
        side_effect=[None, None, None, None, None, "interrupt"],
    ):
        result = run_ops(pane, "$1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 0)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts.count("u") == 2


def test_run_ops_paused_saves_remaining_ops_from_the_interrupted_one(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    ops = [
        EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",)),
        EditOp(kind="insert", start_line=5, end_line=4, new_lines=("b",)),
    ]
    # op[0]'s insert-half is ["gg", "O", "a", "Escape"] — 4 checks, all None,
    # so it completes fully. op[1]'s delete-half is empty (pure insert), so
    # its insert-half's very first check is next — "pause" fires there,
    # before anything of op[1] is sent.
    with patch(
        "vim_ai_follower.control.check_signal", side_effect=[None, None, None, None, "pause"]
    ):
        result = run_ops(pane, "$1", ops, pace_seconds=0.1, base_dir=tmp_path)
    assert result == AnimationResult("paused", 1)
    pending = control.load_pending_animation("$1", base_dir=tmp_path)
    assert isinstance(pending, control.PendingApplyEdit)
    assert pending.ops == [ops[1]]
    assert pending.pace_seconds == 0.1


def test_run_lines_clears_stale_signals_before_starting(tmp_path: Path) -> None:
    control.request_interrupt("$1", base_dir=tmp_path)
    pane = cast(TmuxPane, MagicMock())
    result = run_lines(pane, "$1", ("a",), pace_seconds=0.0, base_dir=tmp_path)
    # the stale interrupt from before this call started must not affect it
    assert result == AnimationResult("completed", 1)


def test_run_lines_all_complete() -> None:
    pane = cast(TmuxPane, MagicMock())
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_lines(pane, "$1", ("a", "b"), pace_seconds=0.0)
    assert result == AnimationResult("completed", 2)


def test_run_lines_interrupted_before_any_line_sent() -> None:
    pane = cast(TmuxPane, MagicMock())
    with (
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_lines(pane, "$1", ("a",), pace_seconds=0.0)
    assert result == AnimationResult("interrupted", 0)
    pane.send_text.assert_not_called()  # type: ignore[attr-defined]
    pane.send_key.assert_called_once_with("Escape")  # type: ignore[attr-defined]


def test_run_lines_types_each_line_on_its_own_line() -> None:
    pane = cast(TmuxPane, MagicMock())
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_lines(pane, "$1", ("a", "b"), pace_seconds=0.0)
    assert result == AnimationResult("completed", 2)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts == ["i", "a", "o", "b"]  # 'o' gives line 2 its own line


def test_run_lines_continuation_opens_even_the_first_line_with_o() -> None:
    pane = cast(TmuxPane, MagicMock())
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_lines(pane, "$1", ("rest",), pace_seconds=0.0, continuation=True)
    assert result == AnimationResult("completed", 1)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts == ["o", "rest"]  # the buffer already has content; 'i' would join lines


def test_run_lines_interrupted_after_bare_i_skips_undo(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    # first check (before "i") is None so "i" is sent; second check (before
    # the line's text) is "interrupt". 'i' + Escape with no text typed never
    # creates an undo entry — sending 'u' here would undo the PREVIOUS change
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, "interrupt"]):
        result = run_lines(pane, "$1", ("a",), pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 0)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts == ["i"]
    pane.send_key.assert_called_once_with("Escape")  # type: ignore[attr-defined]


def test_run_lines_interrupted_after_text_undoes_the_partial_line(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    # "i" and the text both go through; interrupt fires before Escape — a
    # real change exists now, so the partial line must be undone
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, None, "interrupt"]):
        result = run_lines(pane, "$1", ("a",), pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 0)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts == ["i", "a", "u"]


def test_run_lines_interrupted_after_bare_o_undoes_the_opened_line(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    # line "a" completes (3 checks); line "b"'s opener "o" goes through and
    # the interrupt fires before its text — 'o' alone already opened a line
    # (a real change), so it must be undone
    with patch(
        "vim_ai_follower.control.check_signal", side_effect=[None, None, None, None, "interrupt"]
    ):
        result = run_lines(pane, "$1", ("a", "b"), pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 1)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts == ["i", "a", "o", "u"]


def test_run_lines_paused_mid_first_line_saves_non_continuation(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    # pause lands before line 0's Escape: 'i' + text were sent, the partial
    # line is undone, and the buffer is back to its virgin blank line — so
    # the resume must open with 'i' again (continuation=False)
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, None, "pause"]):
        result = run_lines(pane, "$1", ("a", "b"), pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("paused", 0)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts == ["i", "a", "u"]
    pending = control.load_pending_animation("$1", base_dir=tmp_path)
    assert pending == control.PendingShowFresh(("a", "b"), 0.0, continuation=False)


def test_run_lines_paused_saves_remaining_lines_as_continuation(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    # line "a" fully completes (3 checks: i, a, Escape); line "b"'s first
    # check (before its own "o") returns "pause" — the buffer keeps line
    # "a", so the resume must open with 'o' (continuation=True)
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, None, None, "pause"]):
        result = run_lines(pane, "$1", ("a", "b", "c"), pace_seconds=0.2, base_dir=tmp_path)
    assert result == AnimationResult("paused", 1)
    pending = control.load_pending_animation("$1", base_dir=tmp_path)
    assert isinstance(pending, control.PendingShowFresh)
    assert pending.lines == ("b", "c")
    assert pending.pace_seconds == 0.2
    assert pending.continuation is True


def test_run_lines_paused_during_a_continuation_run_stays_continuation(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    # a resumed run paused again at its very first line must NOT flip back
    # to 'i': the buffer still has the previously-typed lines
    with patch("vim_ai_follower.control.check_signal", side_effect=["pause"]):
        result = run_lines(
            pane, "$1", ("x", "y"), pace_seconds=0.0, base_dir=tmp_path, continuation=True
        )
    assert result == AnimationResult("paused", 0)
    pending = control.load_pending_animation("$1", base_dir=tmp_path)
    assert pending == control.PendingShowFresh(("x", "y"), 0.0, continuation=True)


def test_run_lines_empty_tuple_completes_immediately(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        result = run_lines(pane, "$1", (), pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("completed", 0)
    pane.send_text.assert_not_called()  # type: ignore[attr-defined]
