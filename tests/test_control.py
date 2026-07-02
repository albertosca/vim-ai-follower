from __future__ import annotations

from pathlib import Path

from vim_ai_follower import control
from vim_ai_follower.diff import EditOp


def test_check_signal_returns_none_when_nothing_set(tmp_path: Path) -> None:
    assert control.check_signal("$1", base_dir=tmp_path) is None


def test_check_signal_returns_and_consumes_pause(tmp_path: Path) -> None:
    control.request_pause("$1", base_dir=tmp_path)
    assert control.check_signal("$1", base_dir=tmp_path) == "pause"
    assert control.check_signal("$1", base_dir=tmp_path) is None


def test_check_signal_returns_and_consumes_interrupt(tmp_path: Path) -> None:
    control.request_interrupt("$1", base_dir=tmp_path)
    assert control.check_signal("$1", base_dir=tmp_path) == "interrupt"
    assert control.check_signal("$1", base_dir=tmp_path) is None


def test_check_signal_interrupt_takes_priority_over_pause(tmp_path: Path) -> None:
    control.request_pause("$1", base_dir=tmp_path)
    control.request_interrupt("$1", base_dir=tmp_path)
    assert control.check_signal("$1", base_dir=tmp_path) == "interrupt"
    # pause is left alone, still pending
    assert control.check_signal("$1", base_dir=tmp_path) == "pause"


def test_clear_signals_removes_both_without_erroring_if_absent(tmp_path: Path) -> None:
    control.clear_signals("$1", base_dir=tmp_path)  # nothing to clear, no error
    control.request_pause("$1", base_dir=tmp_path)
    control.request_interrupt("$1", base_dir=tmp_path)
    control.clear_signals("$1", base_dir=tmp_path)
    assert control.check_signal("$1", base_dir=tmp_path) is None


def test_has_pending_animation_false_when_none_saved(tmp_path: Path) -> None:
    assert control.has_pending_animation("$1", base_dir=tmp_path) is False


def test_save_and_load_pending_apply_edit(tmp_path: Path) -> None:
    ops = [EditOp(kind="replace", start_line=2, end_line=2, new_lines=("X",))]
    control.save_pending_apply_edit("$1", ops, 0.05, base_dir=tmp_path)
    assert control.has_pending_animation("$1", base_dir=tmp_path) is True

    loaded = control.load_pending_animation("$1", base_dir=tmp_path)
    assert isinstance(loaded, control.PendingApplyEdit)
    assert loaded.ops == ops
    assert loaded.pace_seconds == 0.05
    # consumed on load
    assert control.has_pending_animation("$1", base_dir=tmp_path) is False


def test_save_and_load_pending_show_fresh(tmp_path: Path) -> None:
    control.save_pending_show_fresh("$1", ("a", "b"), 0.1, base_dir=tmp_path)
    loaded = control.load_pending_animation("$1", base_dir=tmp_path)
    assert isinstance(loaded, control.PendingShowFresh)
    assert loaded.lines == ("a", "b")
    assert loaded.pace_seconds == 0.1


def test_load_pending_animation_returns_none_when_absent(tmp_path: Path) -> None:
    assert control.load_pending_animation("$1", base_dir=tmp_path) is None
