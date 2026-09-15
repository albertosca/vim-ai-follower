from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends import get_follower
from vim_ai_follower.backends.nvim import NvimFollower, _animate_lines
from vim_ai_follower.diff import EditOp, compute_edit_script

# --- _animate_lines driver (mocked Nvim) ---------------------------------


@pytest.fixture(autouse=True)
def _no_sleep() -> Iterator[None]:
    # The per-char animation sleeps `pace` between characters; drop the real
    # delay so unit tests exercise the loop instantly.
    with patch("vim_ai_follower.backends.nvim.time.sleep"):
        yield


def test_animate_lines_types_each_line_char_by_char() -> None:
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        result = _animate_lines(
            nvim, 7, ("a", "bb"), 0, lambda: 0.05, "@1", ns=3, base_dir=Path("/tmp/x")
        )
    assert result == AnimationResult("completed", 2)
    # each line is seeded blank at its row, then typed one character at a time
    assert nvim.api.buf_set_lines.call_args_list == [
        call(7, 0, 0, True, [""]),
        call(7, 1, 1, True, [""]),
    ]
    assert nvim.api.buf_set_text.call_args_list == [
        call(7, 0, 0, 0, 0, ["a"]),
        call(7, 1, 0, 1, 0, ["b"]),
        call(7, 1, 1, 1, 1, ["b"]),
    ]


def test_animate_lines_forces_the_static_default_colorscheme() -> None:
    # Alberto's request (2026-09-15): a static, hardcoded colorscheme so
    # the animation never looks different from his own — his real
    # ~/.config/nvim/init.vim sources ~/.vimrc (gruvbox/dark), but that
    # config's own plugin loading can still be mid-flight when the
    # follower starts typing. Force it explicitly instead of racing it.
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        _animate_lines(nvim, 7, ("a",), 0, lambda: 0.05, "@1", ns=3, base_dir=Path("/tmp/x"))
    assert call("silent! colorscheme gruvbox") in nvim.command.call_args_list
    assert call("set background=dark") in nvim.command.call_args_list


def test_animate_lines_pace_zero_types_the_whole_line_at_once() -> None:
    # The pace-0 catch-up (des-interrupt / crash replay) must not animate: one
    # whole-line insert per line, no per-char loop.
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        _animate_lines(nvim, 7, ("x",), 4, lambda: 0.0, "@1", ns=1)
    nvim.api.buf_set_lines.assert_any_call(7, 4, 4, True, [""])
    nvim.api.buf_set_text.assert_called_once_with(7, 4, 0, 4, 0, ["x"])


def test_animate_lines_checks_signal_before_each_line() -> None:
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None) as check:
        _animate_lines(nvim, 7, ("a", "b"), 0, lambda: 0.0, "@9", ns=1, base_dir=Path("/d"))
    # pace 0 -> only the per-line boundary checks (no per-char loop)
    assert check.call_args_list == [call("@9", Path("/d")), call("@9", Path("/d"))]


def test_animate_lines_interrupts_before_typing_when_signal_fires_immediately() -> None:
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value="interrupt"):
        result = _animate_lines(nvim, 7, ("a", "b"), 0, lambda: 0.0, "@1", ns=1)
    assert result == AnimationResult("interrupted", 0)
    nvim.api.buf_set_text.assert_not_called()


def test_animate_lines_stops_partway_when_interrupt_fires_between_lines() -> None:
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, "interrupt"]):
        result = _animate_lines(nvim, 7, ("a", "b", "c"), 0, lambda: 0.0, "@1", ns=1)
    assert result == AnimationResult("interrupted", 1)
    assert nvim.api.buf_set_text.call_count == 1  # only line 0 was typed


def test_animate_lines_snaps_the_line_on_a_mid_char_interrupt() -> None:
    # Interrupt detected mid-line (per-char check): the untyped remainder is
    # snapped in so the buffer lands on a clean line boundary, and the line
    # counts as done (index + 1).
    nvim = MagicMock()
    # line 0 "hi" typed fully, then on line 1 "abc" type 'a', interrupt before 'b'
    signals = [None, None, None, None, None, "interrupt"]
    with patch("vim_ai_follower.control.check_signal", side_effect=signals):
        result = _animate_lines(nvim, 7, ("hi", "abc"), 0, lambda: 0.05, "@1", ns=1)
    assert result == AnimationResult("interrupted", 2)
    nvim.api.buf_set_text.assert_any_call(7, 1, 1, 1, 1, ["bc"])  # snapped remainder


def test_animate_lines_pause_inside_the_char_loop_then_resumes(tmp_path: Path) -> None:
    # Pause mid-line (per-char check) blocks in _wait_while_paused; the resume
    # toggle then continues typing the same line from the same character.
    nvim = MagicMock()
    with (
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=[None, "pause", "pause", None, None],
        ),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = _animate_lines(nvim, 7, ("ab",), 0, lambda: 0.05, "@1", ns=1)
    assert result == AnimationResult("completed", 1)
    assert nvim.api.buf_set_text.call_args_list == [
        call(7, 0, 0, 0, 0, ["a"]),
        call(7, 0, 1, 0, 1, ["b"]),
    ]


def test_animate_lines_interrupt_during_a_mid_line_pause_snaps_and_stops(tmp_path: Path) -> None:
    # Paused mid-line, then interrupted: _wait_while_paused returns False, the
    # line is snapped whole, and the run reports interrupted with it counted.
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause", "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = _animate_lines(nvim, 7, ("ab",), 0, lambda: 0.05, "@1", ns=1)
    assert result == AnimationResult("interrupted", 1)
    nvim.api.buf_set_text.assert_any_call(7, 0, 0, 0, 0, ["ab"])  # snapped whole


def test_animate_lines_pace_zero_skips_empty_lines(tmp_path: Path) -> None:
    # A blank line in the pace-0 catch-up path is just the seeded "" — no
    # whole-line insert for it.
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        result = _animate_lines(nvim, 7, ("a", "", "b"), 0, lambda: 0.0, "@1", ns=1)
    assert result == AnimationResult("completed", 3)
    assert nvim.api.buf_set_text.call_count == 2  # the empty line inserts nothing


def test_animate_lines_pauses_then_resumes_retyping_the_same_line(tmp_path: Path) -> None:
    # "pause" at line 1's boundary blocks in _wait_while_paused; the next poll
    # returns "pause" (the resume toggle), then line 1 is finally typed.
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause", "pause", None]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = _animate_lines(nvim, 7, ("a", "b"), 0, lambda: 0.0, "@1", ns=1)
    assert result == AnimationResult("completed", 2)
    assert nvim.api.buf_set_text.call_count == 2  # both lines typed, none skipped


def test_animate_lines_pause_then_interrupt_returns_interrupted(tmp_path: Path) -> None:
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.control.check_signal", side_effect=["pause", "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = _animate_lines(nvim, 7, ("a", "b"), 0, lambda: 0.0, "@1", ns=1)
    assert result == AnimationResult("interrupted", 0)
    nvim.api.buf_set_text.assert_not_called()


def test_animate_lines_saves_the_remainder_while_paused(tmp_path: Path) -> None:
    nvim = MagicMock()
    saved: list[int] = []
    with (
        patch("vim_ai_follower.control.check_signal", side_effect=["pause", "pause", None]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        _animate_lines(nvim, 7, ("a",), 0, lambda: 0.0, "@1", ns=1, save_pending=saved.append)
    assert saved == [0]  # the crash-fallback remainder starts at the paused line


def test_animate_lines_empty_completes_without_typing() -> None:
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        result = _animate_lines(nvim, 7, (), 0, lambda: 0.0, "@1", ns=1)
    assert result == AnimationResult("completed", 0)
    nvim.api.buf_set_text.assert_not_called()


# --- NvimFollower (mocked pynvim.attach) ---------------------------------


def test_is_alive_true_when_connection_succeeds() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=MagicMock()):
        assert follower.is_alive() is True


def test_is_alive_false_when_socket_missing() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    with patch(
        "vim_ai_follower.backends.nvim.pynvim.attach",
        side_effect=OSError("no such file"),
    ):
        assert follower.is_alive() is False


def test_show_fresh_never_edits_the_real_file_and_types_the_content(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.current.buffer.handle = 7
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.show_fresh("/tmp/f.py", "hello\nworld\n")
    assert result == AnimationResult("completed", 2)
    # Never `:e`/`:edit` the real file — that would flash the finished content.
    issued = [c.args[0] for c in nvim.command.call_args_list]
    assert not any(cmd.startswith("edit ") or cmd.startswith("e ") for cmd in issued)
    # Each line is typed at its row (pace 0 here -> whole-line inserts).
    nvim.api.buf_set_text.assert_any_call(7, 0, 0, 0, 0, ["hello"])
    nvim.api.buf_set_text.assert_any_call(7, 1, 0, 1, 0, ["world"])


def test_show_fresh_marks_and_clears_animating(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.current.buffer.handle = 7
    seen: list[bool] = []

    def _check(window_id: str, base_dir: Path | None = None) -> None:
        from vim_ai_follower import control

        seen.append(control.is_animating("@1", tmp_path))
        return

    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=_check),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        follower.show_fresh("/tmp/f.py", "a\nb\n")
    from vim_ai_follower import control

    assert seen and all(seen)
    assert control.is_animating("@1", tmp_path) is False


def test_show_fresh_relocks_buffer_when_completed(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.current.buffer.handle = 7
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        follower.show_fresh("/tmp/f.py", "a\n")
    # unlocked around the animation, then relocked (nomodifiable) after.
    assert nvim.api.buf_set_option.call_args_list[0] == call(7, "modifiable", True)
    assert nvim.api.buf_set_option.call_args_list[-1] == call(7, "modifiable", False)


def test_show_fresh_leaves_buffer_modifiable_when_interrupted(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.current.buffer.handle = 7
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.show_fresh("/tmp/f.py", "a\nb\n")
    assert result == AnimationResult("interrupted", 0)
    # never relocked — the user owns the buffer after an interrupt (hand-over).
    assert call(7, "modifiable", False) not in nvim.api.buf_set_option.call_args_list


def test_show_fresh_pause_saves_show_fresh_remainder_then_resumes(tmp_path: Path) -> None:
    from vim_ai_follower import control

    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.current.buffer.handle = 7
    saved: list[tuple[tuple[str, ...], bool, str]] = []
    real_save = control.save_pending_show_fresh

    def spy_save(
        window_id: str,
        lines: tuple[str, ...],
        pace: float,
        continuation: bool = False,
        base_dir: Path | None = None,
        file_path: str = "",
    ) -> None:
        saved.append((lines, continuation, file_path))
        real_save(window_id, lines, pace, continuation=continuation, file_path=file_path)

    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        # line 0 typed, pause at line 1's boundary, resume, then line 1 typed.
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause", "pause", None]),
        patch("vim_ai_follower.control.save_pending_show_fresh", side_effect=spy_save),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.show_fresh("/tmp/f.py", "a\nb\n")
    assert result == AnimationResult("completed", 2)
    # The remainder saved while paused is the still-untyped tail, as a
    # continuation (earlier lines already landed), tagged with the file.
    assert saved == [(("b",), True, "/tmp/f.py")]
    # Resume disarms the crash fallback.
    assert control.has_pending_animation("@1", tmp_path) is False


def test_apply_edit_pause_at_op_boundary_saves_ops_and_resumes(tmp_path: Path) -> None:
    from vim_ai_follower import control

    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    saved: list[int] = []
    real_save = control.save_pending_apply_edit

    def spy_save(
        window_id: str,
        ops: list[EditOp],
        pace: float,
        base_dir: Path | None = None,
        file_path: str = "",
    ) -> None:
        saved.append(len(ops))
        real_save(window_id, ops, pace, file_path=file_path)

    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        # pause at op0's boundary, resume, then op0 runs (delete + line "a").
        patch("vim_ai_follower.control.check_signal", side_effect=["pause", "pause", None, None]),
        patch("vim_ai_follower.control.save_pending_apply_edit", side_effect=spy_save),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("completed", 1)
    assert saved == [1]  # remainder = the whole op list from the paused op
    nvim.api.buf_set_text.assert_called_once()


def test_apply_edit_pause_at_op_boundary_then_interrupt(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=["pause", "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("interrupted", 0)
    nvim.api.buf_set_text.assert_not_called()  # interrupted before op0's delete


def test_apply_edit_pause_inside_an_ops_lines_saves_and_resumes(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a", "b"))
    saved: list[int] = []

    def spy_save(
        window_id: str,
        ops: list[EditOp],
        pace: float,
        base_dir: Path | None = None,
        file_path: str = "",
    ) -> None:
        saved.append(len(ops))

    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        # op boundary, line0 typed, pause at line1, resume, line1 typed.
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=[None, None, "pause", "pause", None],
        ),
        patch("vim_ai_follower.control.save_pending_apply_edit", side_effect=spy_save),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("completed", 1)
    assert saved == [1]  # the op-granular remainder is saved mid-op too
    assert nvim.api.buf_set_text.call_count == 2  # both lines eventually typed


def test_drive_skips_relock_for_an_adopted_follower(tmp_path: Path) -> None:
    from vim_ai_follower import state

    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.current.buffer.handle = 7
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=True)
        follower.show_fresh("/tmp/f.py", "a\n")
    # Unlocked for the animation, but an adopted (user-owned) nvim is never
    # relocked afterwards.
    assert nvim.api.buf_set_option.call_args_list[0] == call(7, "modifiable", True)
    assert call(7, "modifiable", False) not in nvim.api.buf_set_option.call_args_list


def test_drive_relocks_a_dedicated_follower_recorded_not_adopted(tmp_path: Path) -> None:
    from vim_ai_follower import state

    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.current.buffer.handle = 7
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=False)
        follower.show_fresh("/tmp/f.py", "a\n")
    assert nvim.api.buf_set_option.call_args_list[-1] == call(7, "modifiable", False)


def test_stop_quits_a_launched_nvim(tmp_path: Path) -> None:
    from vim_ai_follower import state

    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=False)
        follower.stop()
    nvim.command.assert_called_once_with("qall!")  # dedicated nvim quit, its split closes


def test_stop_is_a_noop_for_an_adopted_nvim(tmp_path: Path) -> None:
    from vim_ai_follower import state

    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=True)
        follower.stop()
    nvim.command.assert_not_called()  # never quit the user's own editor


def test_apply_edit_deletes_then_animates_each_op(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    ops = compute_edit_script("hello\nworld\n", "hello\nvim ai follower\n")
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", ops)
    assert result == AnimationResult("completed", 1)
    # replace of line 2: delete old line then type the new one (pace 0 -> whole line).
    nvim.api.buf_set_lines.assert_any_call(7, 1, 2, True, [])
    nvim.api.buf_set_text.assert_any_call(7, 1, 0, 1, 0, ["vim ai follower"])


def test_apply_edit_interrupted_reports_completed_op_index(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    ops = [
        EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",)),
        EditOp(kind="insert", start_line=3, end_line=2, new_lines=("b",)),
    ]
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        # op0: boundary + line0 between + line0 char0 (types "a"); then op1's
        # boundary interrupts -> completed op index 1.
        patch("vim_ai_follower.control.check_signal", side_effect=[None, None, None, "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", ops)
    assert result == AnimationResult("interrupted", 1)


def test_apply_edit_interrupted_inside_an_ops_line_animation(tmp_path: Path) -> None:
    # The signal fires WITHIN _animate_lines for op0's second line (not at an
    # op boundary), so apply_edit must surface interrupted at that op's index.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a", "b"))
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        # op boundary (None), line0 (None, typed), line1 boundary -> interrupt.
        patch("vim_ai_follower.control.check_signal", side_effect=[None, None, "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("interrupted", 0)
    assert nvim.api.buf_set_text.call_count == 1  # line0 snapped whole on interrupt


def test_apply_edit_pure_delete_op_needs_no_animation(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    op = EditOp(kind="delete", start_line=2, end_line=3, new_lines=())
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [op])
    assert result == AnimationResult("completed", 1)
    nvim.api.buf_set_lines.assert_any_call(7, 1, 3, True, [])
    nvim.api.buf_set_text.assert_not_called()


def test_pace_provider_falls_back_to_constructed_pace_without_window_id() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", pace_seconds=0.07)
    assert follower._pace_provider() == 0.07


def test_pace_provider_reads_live_speed_from_state(tmp_path: Path) -> None:
    from vim_ai_follower import config as config_mod
    from vim_ai_follower import state

    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.99)
    with patch("vim_ai_follower.cache.CACHE_DIR", tmp_path):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", speed="lento")
        assert follower._pace_provider() == config_mod.pace_seconds_for("lento")


def test_goto_line_sets_cursor() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.goto_line(3)
    assert nvim.current.window.cursor == (3, 0)


def test_close_tab_wipes_the_buffer_found_via_bufnr() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = 9
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.close_tab("/tmp/f.py")
    nvim.funcs.bufnr.assert_called_once_with("/tmp/f.py")
    nvim.command.assert_called_once_with("silent! bwipeout! 9")


def test_close_tab_is_a_noop_when_bufnr_finds_nothing() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = -1
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.close_tab("/tmp/f.py")
    nvim.command.assert_not_called()


def test_goto_file_switches_to_an_existing_buffer_via_bufnr() -> None:
    # bufnr() applies nvim's own path canonicalization (e.g. macOS's
    # /tmp -> /private/tmp symlink), unlike a raw string compare against
    # nvim_buf_get_name — see the docstring on goto_file.
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = 9
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.goto_file("/tmp/f.py")
    nvim.funcs.bufnr.assert_called_once_with("/tmp/f.py")
    nvim.api.set_current_buf.assert_called_once_with(9)
    nvim.api.create_buf.assert_not_called()


def test_goto_file_creates_and_names_a_new_buffer_when_bufnr_finds_nothing() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = -1
    nvim.api.create_buf.return_value = 42
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.goto_file("/tmp/f.py")
    nvim.api.create_buf.assert_called_once_with(True, False)
    nvim.api.buf_set_name.assert_called_once_with(42, "/tmp/f.py")
    nvim.api.set_current_buf.assert_called_once_with(42)


def test_ensure_showing_delegates_to_goto_file() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.funcs.bufnr.return_value = 9
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        follower.ensure_showing("/tmp/f.py")
    nvim.api.set_current_buf.assert_called_once_with(9)


def test_stop_is_a_noop() -> None:
    NvimFollower(socket_path="/tmp/x.sock").stop()


def test_get_follower_returns_nvim_follower() -> None:
    follower = get_follower("nvim", "/tmp/x.sock", pace_seconds=0.02, window_id="@1")
    assert isinstance(follower, NvimFollower)
    assert follower.socket_path == "/tmp/x.sock"
    assert follower.window_id == "@1"
    assert follower.pace_seconds == 0.02


def test_get_follower_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        get_follower("bogus", "x")
