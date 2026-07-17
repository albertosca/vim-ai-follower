from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from vim_ai_follower import control
from vim_ai_follower.diff import EditOp


def test_check_signal_returns_none_when_nothing_set(tmp_path: Path) -> None:
    assert control.check_signal("@1", base_dir=tmp_path) is None


def test_check_signal_returns_and_consumes_pause(tmp_path: Path) -> None:
    control.request_pause("@1", base_dir=tmp_path)
    assert control.check_signal("@1", base_dir=tmp_path) == "pause"
    assert control.check_signal("@1", base_dir=tmp_path) is None


def test_check_signal_returns_and_consumes_interrupt(tmp_path: Path) -> None:
    control.request_interrupt("@1", base_dir=tmp_path)
    assert control.check_signal("@1", base_dir=tmp_path) == "interrupt"
    assert control.check_signal("@1", base_dir=tmp_path) is None


def test_check_signal_interrupt_takes_priority_over_pause(tmp_path: Path) -> None:
    control.request_pause("@1", base_dir=tmp_path)
    control.request_interrupt("@1", base_dir=tmp_path)
    assert control.check_signal("@1", base_dir=tmp_path) == "interrupt"
    # pause is left alone, still pending
    assert control.check_signal("@1", base_dir=tmp_path) == "pause"


def test_clear_signals_removes_both_without_erroring_if_absent(tmp_path: Path) -> None:
    control.clear_signals("@1", base_dir=tmp_path)  # nothing to clear, no error
    control.request_pause("@1", base_dir=tmp_path)
    control.request_interrupt("@1", base_dir=tmp_path)
    control.clear_signals("@1", base_dir=tmp_path)
    assert control.check_signal("@1", base_dir=tmp_path) is None


def test_has_pending_animation_false_when_none_saved(tmp_path: Path) -> None:
    assert control.has_pending_animation("@1", base_dir=tmp_path) is False


def test_save_and_load_pending_apply_edit(tmp_path: Path) -> None:
    ops = [EditOp(kind="replace", start_line=2, end_line=2, new_lines=("X",))]
    control.save_pending_apply_edit("@1", ops, 0.05, base_dir=tmp_path)
    assert control.has_pending_animation("@1", base_dir=tmp_path) is True

    loaded = control.load_pending_animation("@1", base_dir=tmp_path)
    assert isinstance(loaded, control.PendingApplyEdit)
    assert loaded.ops == ops
    assert loaded.pace_seconds == 0.05
    # consumed on load
    assert control.has_pending_animation("@1", base_dir=tmp_path) is False


def test_save_and_load_pending_show_fresh(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("a", "b"), 0.1, base_dir=tmp_path)
    loaded = control.load_pending_animation("@1", base_dir=tmp_path)
    assert isinstance(loaded, control.PendingShowFresh)
    assert loaded.lines == ("a", "b")
    assert loaded.pace_seconds == 0.1


def test_pending_show_fresh_round_trips_continuation(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("x",), 0.05, continuation=True, base_dir=tmp_path)
    pending = control.load_pending_animation("@1", tmp_path)
    assert pending == control.PendingShowFresh(("x",), 0.05, continuation=True)


def test_pending_show_fresh_defaults_continuation_false(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("x",), 0.05, base_dir=tmp_path)
    pending = control.load_pending_animation("@1", tmp_path)
    assert pending == control.PendingShowFresh(("x",), 0.05, continuation=False)


def test_pending_round_trips_file_path(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("a",), 0.03, base_dir=tmp_path, file_path="/tmp/f.py")
    pending = control.load_pending_animation("@1", base_dir=tmp_path)
    assert pending is not None and pending.file_path == "/tmp/f.py"


def test_pending_defaults_file_path_for_old_payloads(tmp_path: Path) -> None:
    control.save_pending_apply_edit("@1", [], 0.03, base_dir=tmp_path)
    pending = control.load_pending_animation("@1", base_dir=tmp_path)
    assert pending is not None and pending.file_path == ""


def test_load_pending_animation_returns_none_when_absent(tmp_path: Path) -> None:
    assert control.load_pending_animation("@1", base_dir=tmp_path) is None


def test_discard_pending_animation_removes_file_without_erroring_if_absent(
    tmp_path: Path,
) -> None:
    control.discard_pending_animation("@1", base_dir=tmp_path)  # no error
    control.save_pending_apply_edit("@1", [], 0.0, base_dir=tmp_path)
    control.discard_pending_animation("@1", base_dir=tmp_path)
    assert control.has_pending_animation("@1", base_dir=tmp_path) is False


def test_check_signal_survives_losing_the_unlink_race(tmp_path: Path) -> None:
    control.request_interrupt("@1", tmp_path)
    real_unlink = Path.unlink

    def racing_unlink(self: Path, missing_ok: bool = False) -> None:
        real_unlink(self, missing_ok=True)  # another process got there first...
        real_unlink(self, missing_ok=missing_ok)  # ...then ours runs on a gone file

    with patch.object(Path, "unlink", racing_unlink):
        # losing the race means the signal was not ours to act on — and the
        # animation process must NOT crash mid-animation over it
        assert control.check_signal("@1", tmp_path) is None


def test_pending_write_is_atomic_no_partial_file_visible(tmp_path: Path) -> None:
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("x",))
    control.save_pending_apply_edit("@1", [op], 0.1, tmp_path)
    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == ["@1.pending_animation.json"]  # no .tmp residue
    # and the write goes through an atomic rename, not a direct write:
    with patch.object(Path, "replace", autospec=True, side_effect=Path.replace) as replace:
        control.save_pending_show_fresh("@1", ("y",), 0.1, base_dir=tmp_path)
    assert replace.called


def test_load_pending_returns_none_when_file_vanishes_mid_read(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("y",), 0.1, base_dir=tmp_path)
    with patch.object(Path, "read_text", side_effect=FileNotFoundError):
        assert control.load_pending_animation("@1", tmp_path) is None
    # the real file is still there for the next, non-racing loader:
    assert control.load_pending_animation("@1", tmp_path) is not None


def test_animating_marker_lifecycle(tmp_path: Path) -> None:
    assert control.is_animating("@1", tmp_path) is False
    control.mark_animating("@1", tmp_path)
    assert control.is_animating("@1", tmp_path) is True  # this test process is alive
    control.clear_animating("@1", tmp_path)
    assert control.is_animating("@1", tmp_path) is False


def test_animating_marker_ignores_dead_process(tmp_path: Path) -> None:
    control.mark_animating("@1", tmp_path)
    # overwrite with a PID that cannot be running (max pid + unlikely)
    (tmp_path / "@1.animating").write_text("99999999")
    assert control.is_animating("@1", tmp_path) is False


def test_animating_marker_ignores_garbage_content(tmp_path: Path) -> None:
    control.mark_animating("@1", tmp_path)
    (tmp_path / "@1.animating").write_text("not-a-pid")
    assert control.is_animating("@1", tmp_path) is False
