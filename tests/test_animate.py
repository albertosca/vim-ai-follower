from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

from vim_ai_follower import control
from vim_ai_follower.animate import (
    PAUSE_POLL_SECONDS,
    AnimationResult,
    ApplyResult,
    KeySequence,
    _delete_sequences,
    _insert_sequences,
    _line_sequences,
    run_lines,
    run_ops,
    send_paced,
)
from vim_ai_follower.diff import EditOp
from vim_ai_follower.tmux import TmuxPane


def test_delete_only_op_deletes_line_by_line_for_the_animation() -> None:
    # One :Nd per line, all at start_line (lines shift up after each), so
    # deletions are paced and visible like insertions instead of vanishing
    # in a single :s,ed (live request, 2026-07-15).
    op = EditOp(kind="delete", start_line=2, end_line=3, new_lines=())
    assert _delete_sequences(op) == [
        KeySequence(":2d", literal=True),
        KeySequence("Enter", literal=False),
        KeySequence(":2d", literal=True),
        KeySequence("Enter", literal=False),
    ]


def test_insert_at_top_uses_gg_open_above() -> None:
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
    assert prefix_len == 2  # gg, O — the cursor-positioning prefix


def test_insert_mid_file_positions_then_opens_below() -> None:
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
    assert prefix_len == 3  # :2, Enter, o


def test_replace_op_deletes_then_inserts() -> None:
    op = EditOp(kind="replace", start_line=2, end_line=2, new_lines=("X",))
    insert_seq, _ = _insert_sequences(op)
    assert _delete_sequences(op) + insert_seq == [
        KeySequence(":2d", literal=True),
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
        result = send_paced(pane, sequences, pace_seconds=0.0, window_id="@1")
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
        result = send_paced(pane, sequences, pace_seconds=0.05, window_id="@1")
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
        result = send_paced(pane, sequences, pace_seconds=1.0, window_id="@1", max_seconds=4.0)
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
        result = send_paced(pane, sequences, pace_seconds=0.0, window_id="@1")
    sleep.assert_not_called()
    monotonic.assert_called_once()  # only the initial deadline computation
    assert result == ApplyResult("completed", 2)


def test_apply_stops_before_sending_when_interrupted_immediately() -> None:
    pane = cast(TmuxPane, MagicMock())
    sequences = [KeySequence("a"), KeySequence("b")]
    with patch("vim_ai_follower.control.check_signal", return_value="interrupt"):
        result = send_paced(pane, sequences, pace_seconds=0.0, window_id="@1")
    pane.send_text.assert_not_called()  # type: ignore[attr-defined]
    assert result == ApplyResult("interrupted", 0)


def test_apply_stops_partway_when_paused_mid_sequence() -> None:
    pane = cast(TmuxPane, MagicMock())
    sequences = [KeySequence("a"), KeySequence("b"), KeySequence("c")]
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, None, "pause"]):
        result = send_paced(pane, sequences, pace_seconds=0.0, window_id="@1")
    assert pane.send_text.call_count == 2  # type: ignore[attr-defined]
    assert result == ApplyResult("paused", 2)


def test_apply_passes_window_id_and_base_dir_to_check_signal() -> None:
    pane = cast(TmuxPane, MagicMock())
    with patch("vim_ai_follower.control.check_signal", return_value=None) as check:
        send_paced(
            pane,
            [KeySequence("a")],
            pace_seconds=0.0,
            window_id="@7",
            control_base_dir=Path("/tmp/x"),
        )
    check.assert_called_once_with("@7", Path("/tmp/x"))


def test_delete_sequences_one_pair_per_line() -> None:
    op = EditOp(kind="delete", start_line=2, end_line=4, new_lines=())
    seqs = _delete_sequences(op)
    assert len(seqs) == 6  # three (":2d", Enter) pairs
    assert all(s.text == ":2d" for s in seqs[::2])
    assert all(s.text == "Enter" and not s.literal for s in seqs[1::2])


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
    control.request_pause("@1", base_dir=tmp_path)
    pane = cast(TmuxPane, MagicMock())
    ops = [EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))]
    result = run_ops(pane, "@1", ops, pace_seconds=0.0, base_dir=tmp_path)
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
        result = run_ops(pane, "@1", ops, pace_seconds=0.0)
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
        result = run_ops(pane, "@1", ops, pace_seconds=0.0)
    assert result == AnimationResult("completed", 2)


def test_run_ops_interrupted_before_first_op_undoes_nothing() -> None:
    pane = cast(TmuxPane, MagicMock())
    ops = [EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))]
    with (
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_ops(pane, "@1", ops, pace_seconds=0.0)
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
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 0)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts == ["gg", "O", "u"]
    pane.send_key.assert_called_once_with("Escape")  # type: ignore[attr-defined]


def test_run_ops_interrupted_before_insert_mode_entered_skips_undo(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a", "b"))
    # signal fires on the very first check inside the insert-half send_paced()
    # call — before "gg" is even sent, so insert mode was never entered
    with patch("vim_ai_follower.control.check_signal", side_effect=["interrupt"]):
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 0)
    pane.send_key.assert_called_once_with("Escape")  # type: ignore[attr-defined]
    pane.send_text.assert_not_called()  # type: ignore[attr-defined]


def test_run_ops_interrupted_during_delete_needs_no_undo(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="delete", start_line=2, end_line=3, new_lines=())
    # the first pair's command text (":2d") gets typed, but the signal fires
    # before its Enter — no pair committed, so Escape alone is enough
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, "interrupt"]):
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 0)
    pane.send_text.assert_called_once_with(":2d")  # type: ignore[attr-defined]
    pane.send_key.assert_called_once_with("Escape")  # type: ignore[attr-defined]


def test_run_ops_pause_in_insert_half_rolls_back_both_halves_then_resumes(
    tmp_path: Path,
) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="replace", start_line=2, end_line=3, new_lines=("X", "Y"))
    pending_seen: list[object] = []
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == 6:  # delete (2 checks) + insert prefix (3) → before "X"
            return "pause"
        if calls["n"] == 7:  # the wait loop's first poll: snapshot, then resume
            loaded = control.load_pending_animation("@1", tmp_path)
            pending_seen.append(loaded)
            assert isinstance(loaded, control.PendingApplyEdit)
            control.save_pending_apply_edit("@1", loaded.ops, 0.0, tmp_path)
            return "pause"
        return None

    with patch("vim_ai_follower.control.check_signal", side_effect=_check):
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("completed", 1)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts.count("u") == 2  # insert undone AND delete undone before waiting
    assert pending_seen == [control.PendingApplyEdit([op], 0.0)]  # crash fallback was armed
    assert control.has_pending_animation("@1", tmp_path) is False  # and disarmed on resume


def test_run_ops_pause_before_insert_prefix_still_rolls_back_the_delete(
    tmp_path: Path,
) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="replace", start_line=2, end_line=3, new_lines=("X",))
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "pause" if calls["n"] in (3, 4) else None  # pause at insert's 1st check, resume

    with patch("vim_ai_follower.control.check_signal", side_effect=_check):
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("completed", 1)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts.count("u") == 1  # only the delete needed rolling back


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
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 0)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts.count("u") == 2


def test_run_ops_interrupt_during_pause_wait_discards_pending(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    ops = [
        EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",)),
        EditOp(kind="insert", start_line=5, end_line=4, new_lines=("b",)),
    ]
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == 5:  # op0 completes (4 checks); pause before op1
            return "pause"
        if calls["n"] == 6:  # interrupt lands while the hook is waiting
            assert control.has_pending_animation("@1", tmp_path) is True
            return "interrupt"
        return None

    with patch("vim_ai_follower.control.check_signal", side_effect=_check):
        result = run_ops(pane, "@1", ops, pace_seconds=0.1, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 1)
    assert control.has_pending_animation("@1", tmp_path) is False  # wait discards on wake


def test_run_lines_clears_stale_signals_before_starting(tmp_path: Path) -> None:
    control.request_interrupt("@1", base_dir=tmp_path)
    pane = cast(TmuxPane, MagicMock())
    result = run_lines(pane, "@1", ("a",), pace_seconds=0.0, base_dir=tmp_path)
    # the stale interrupt from before this call started must not affect it
    assert result == AnimationResult("completed", 1)


def test_run_lines_all_complete() -> None:
    pane = cast(TmuxPane, MagicMock())
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_lines(pane, "@1", ("a", "b"), pace_seconds=0.0)
    assert result == AnimationResult("completed", 2)


def test_run_lines_interrupted_before_any_line_sent() -> None:
    pane = cast(TmuxPane, MagicMock())
    with (
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_lines(pane, "@1", ("a",), pace_seconds=0.0)
    assert result == AnimationResult("interrupted", 0)
    pane.send_text.assert_not_called()  # type: ignore[attr-defined]
    pane.send_key.assert_called_once_with("Escape")  # type: ignore[attr-defined]


def test_run_lines_types_each_line_on_its_own_line() -> None:
    pane = cast(TmuxPane, MagicMock())
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_lines(pane, "@1", ("a", "b"), pace_seconds=0.0)
    assert result == AnimationResult("completed", 2)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts == ["i", "a", "o", "b"]  # 'o' gives line 2 its own line


def test_run_lines_continuation_opens_even_the_first_line_with_o() -> None:
    pane = cast(TmuxPane, MagicMock())
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_lines(pane, "@1", ("rest",), pace_seconds=0.0, continuation=True)
    assert result == AnimationResult("completed", 1)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts == ["o", "rest"]  # the buffer already has content; 'i' would join lines


def test_run_lines_interrupted_after_bare_i_skips_undo(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    # first check (before "i") is None so "i" is sent; second check (before
    # the line's text) is "interrupt". 'i' + Escape with no text typed never
    # creates an undo entry — sending 'u' here would undo the PREVIOUS change
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, "interrupt"]):
        result = run_lines(pane, "@1", ("a",), pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 0)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts == ["i"]
    pane.send_key.assert_called_once_with("Escape")  # type: ignore[attr-defined]


def test_run_lines_interrupted_after_text_undoes_the_partial_line(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    # "i" and the text both go through; interrupt fires before Escape — a
    # real change exists now, so the partial line must be undone
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, None, "interrupt"]):
        result = run_lines(pane, "@1", ("a",), pace_seconds=0.0, base_dir=tmp_path)
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
        result = run_lines(pane, "@1", ("a", "b"), pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 1)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts == ["i", "a", "o", "u"]


def test_run_lines_pause_mid_first_line_waits_then_retries_with_i(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    pending_seen: list[object] = []
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == 3:  # 'i' + text sent; pause before line 0's Escape
            return "pause"
        if calls["n"] == 4:  # wait poll: snapshot fallback state, resume
            pending_seen.append(control.load_pending_animation("@1", tmp_path))
            return "pause"
        return None

    with patch("vim_ai_follower.control.check_signal", side_effect=_check):
        result = run_lines(pane, "@1", ("a", "b"), pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("completed", 2)
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    # rollback undid the partial line; the retry re-opens with 'i' (virgin buffer)
    assert sent_texts == ["i", "a", "u", "i", "a", "o", "b"]
    assert pending_seen == [control.PendingShowFresh(("a", "b"), 0.0, continuation=False)]


def test_run_lines_pause_on_later_line_arms_continuation_fallback(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    pending_seen: list[object] = []
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == 4:  # line "a" completed (3 checks); pause before line "b"
            return "pause"
        if calls["n"] == 5:
            pending_seen.append(control.load_pending_animation("@1", tmp_path))
            return "pause"
        return None

    with patch("vim_ai_follower.control.check_signal", side_effect=_check):
        result = run_lines(pane, "@1", ("a", "b", "c"), pace_seconds=0.2, base_dir=tmp_path)
    assert result == AnimationResult("completed", 3)
    assert pending_seen == [control.PendingShowFresh(("b", "c"), 0.2, continuation=True)]


def test_run_lines_interrupt_during_pause_wait_of_continuation_run(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    with patch("vim_ai_follower.control.check_signal", side_effect=["pause", "interrupt"]):
        result = run_lines(
            pane, "@1", ("x", "y"), pace_seconds=0.0, base_dir=tmp_path, continuation=True
        )
    assert result == AnimationResult("interrupted", 0)
    assert control.has_pending_animation("@1", tmp_path) is False


def test_run_lines_empty_tuple_completes_immediately(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        result = run_lines(pane, "@1", (), pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("completed", 0)
    pane.send_text.assert_not_called()  # type: ignore[attr-defined]


def test_run_lines_marks_animating_for_the_duration(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    seen: list[bool] = []

    def _check(window_id: str, base_dir: Path | None = None) -> None:
        seen.append(control.is_animating("@1", tmp_path))
        return

    with patch("vim_ai_follower.control.check_signal", side_effect=_check):
        result = run_lines(pane, "@1", ("a", "b"), pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("completed", 2)
    assert seen and all(seen)  # marker present at every keystroke check
    assert control.is_animating("@1", tmp_path) is False  # cleared on the way out


def test_run_ops_clears_animating_after_interrupt_during_wait(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    with patch("vim_ai_follower.control.check_signal", side_effect=["pause", "interrupt"]):
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 0)
    assert control.is_animating("@1", tmp_path) is False


def test_run_ops_pause_wait_polls_until_the_resume_arrives(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return "pause"
        if calls["n"] == 3:  # second wait poll: one idle poll (and sleep) happened
            return "pause"
        return None

    with (
        patch("vim_ai_follower.control.check_signal", side_effect=_check),
        patch("vim_ai_follower.animate.time.sleep") as sleep,
    ):
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("completed", 1)
    sleep.assert_any_call(PAUSE_POLL_SECONDS)


def test_run_ops_interrupt_during_delete_half_pause_wait(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="replace", start_line=2, end_line=3, new_lines=("X",))
    # pause before the delete's Enter (check 2); interrupt lands in the wait
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause", "interrupt"]):
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result == AnimationResult("interrupted", 0)
    assert control.has_pending_animation("@1", tmp_path) is False


def test_run_ops_calls_on_resume_exactly_once_when_resumed_from_a_pause(
    tmp_path: Path,
) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    calls = {"n": 0}

    def _pause_then_resume(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "pause" if calls["n"] in (1, 2) else None

    on_resume = MagicMock()
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_then_resume):
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path, on_resume=on_resume)
    assert result == AnimationResult("completed", 1)
    on_resume.assert_called_once_with()


def test_run_ops_calls_on_resume_when_paused_in_the_delete_half(tmp_path: Path) -> None:
    # A replace op runs a delete half before the insert half; pausing on the
    # very first signal check lands the pause inside the delete-half send_paced,
    # the branch the insert-only case above never reaches.
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="replace", start_line=1, end_line=1, new_lines=("X",))
    calls = {"n": 0}

    def _pause_then_resume(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "pause" if calls["n"] in (1, 2) else None

    on_resume = MagicMock()
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_then_resume):
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path, on_resume=on_resume)
    assert result == AnimationResult("completed", 1)
    on_resume.assert_called_once_with()


def test_run_ops_does_not_call_on_resume_when_never_paused(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    on_resume = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path, on_resume=on_resume)
    assert result == AnimationResult("completed", 1)
    on_resume.assert_not_called()


def test_run_lines_calls_on_resume_exactly_once_when_resumed_from_a_pause(
    tmp_path: Path,
) -> None:
    pane = cast(TmuxPane, MagicMock())
    calls = {"n": 0}

    def _pause_then_resume(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "pause" if calls["n"] in (1, 2) else None

    on_resume = MagicMock()
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_then_resume):
        result = run_lines(
            pane, "@1", ("hello",), pace_seconds=0.0, base_dir=tmp_path, on_resume=on_resume
        )
    assert result == AnimationResult("completed", 1)
    on_resume.assert_called_once_with()


def test_run_lines_re_reads_pace_from_provider_each_line(tmp_path: Path) -> None:
    # pace_seconds accepts a zero-arg callable too — evaluated once per line
    # (a "line boundary"), not once for the whole run, so a mid-run speed
    # change (Ctrl+a +/-) takes effect on the very next line.
    pane = cast(TmuxPane, MagicMock())
    calls: list[int] = []

    def provider() -> float:
        calls.append(1)
        return 0.0

    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_lines(pane, "@1", ("a", "b"), provider, base_dir=tmp_path)
    assert result == AnimationResult("completed", 2)
    assert len(calls) >= 2  # one evaluation per line, not one per run


def test_run_ops_re_reads_pace_from_provider_once_per_op(tmp_path: Path) -> None:
    # Same cadence for run_ops: one evaluation per op (shared by its delete
    # and insert halves), not once for the whole batch of ops.
    pane = cast(TmuxPane, MagicMock())
    ops = [
        EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",)),
        EditOp(kind="insert", start_line=2, end_line=0, new_lines=("b",)),
    ]
    calls: list[int] = []

    def provider() -> float:
        calls.append(1)
        return 0.0

    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.control.clear_signals"),
    ):
        result = run_ops(pane, "@1", ops, provider, base_dir=tmp_path)
    assert result == AnimationResult("completed", 2)
    assert len(calls) >= 2  # one evaluation per op, not one per run


def test_delete_half_pause_rolls_back_each_committed_line_delete(tmp_path: Path) -> None:
    # Three-line delete, pause after the second (":2d", Enter) pair: two
    # deletes committed -> exactly two "u" before the wait, so the buffer
    # sits back on the clean op boundary for the retry.
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="delete", start_line=2, end_line=4, new_lines=())
    calls = {"n": 0}

    def _pause_after_two_pairs(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        # checks precede each sequence: pause at the 5th (before the 3rd
        # pair's ":2d"), resume at the next check inside the wait.
        return "pause" if calls["n"] in (5, 6) else None

    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_after_two_pairs):
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result.outcome == "completed"
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    first_retry = sent_texts.index(":2d", sent_texts.index("u"))
    assert sent_texts[:first_retry].count("u") == 2  # one per committed line delete


def test_insert_half_rollback_undoes_every_line_of_the_delete_half(tmp_path: Path) -> None:
    # replace op spanning three lines; interrupt during the insert half ->
    # rollback must undo the opened insert line (1 u) AND all three
    # per-line deletes (3 u), not just one.
    pane = cast(TmuxPane, MagicMock())
    op = EditOp(kind="replace", start_line=2, end_line=4, new_lines=("x",))
    calls = {"n": 0}

    def _interrupt_in_insert(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        # delete half = 6 sequences (checks 1-6); insert prefix ":1",
        # Enter, "o" = checks 7-9; interrupt at check 10.
        return "interrupt" if calls["n"] == 10 else None

    with patch("vim_ai_follower.control.check_signal", side_effect=_interrupt_in_insert):
        result = run_ops(pane, "@1", [op], pace_seconds=0.0, base_dir=tmp_path)
    assert result.outcome == "interrupted"
    sent_texts = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent_texts.count("u") == 4  # 1 insert + 3 line deletes
