from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from helpers import fresh_buffer, route_exec_lua, shows_the_buffer, tabnew_adds_a_tab

from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends import get_follower
from vim_ai_follower.backends.nvim import (
    _FIND_BUFFER_LUA,
    _FOLLOWER_WINDOW_LUA,
    _READ_THEN_CLEAR_LUA,
    NvimFollower,
    _animate_lines,
    _fileformat_of,
    _format_from_disk,
)
from vim_ai_follower.diff import EditOp, compute_edit_script
from vim_ai_follower.state import FollowerState

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
            nvim,
            7,
            ("a", "bb"),
            0,
            lambda: 0.05,
            "@1",
            namespace_id=3,
            base_directory=Path("/tmp/x"),
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


def test_animate_lines_moves_the_cursor_of_the_window_showing_the_buffer() -> None:
    # Never window 0 (the user's current window in an adopted nvim, T24a
    # review m6): the window comes from _FOLLOWER_WINDOW_LUA, by handle.
    nvim = MagicMock()
    nvim.exec_lua.return_value = 1004
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        _animate_lines(nvim, 7, ("ab",), 0, lambda: 0.05, "@1", namespace_id=3, dedicated=True)
    assert nvim.exec_lua.call_args_list == [call(_FOLLOWER_WINDOW_LUA, 7, 0, False)]
    assert nvim.api.win_set_cursor.call_args_list == [call(1004, [1, 1]), call(1004, [1, 2])]


def test_animate_lines_moves_no_cursor_when_the_buffer_shows_nowhere() -> None:
    nvim = MagicMock()
    nvim.exec_lua.return_value = 0
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        result = _animate_lines(
            nvim, 7, ("ab",), 0, lambda: 0.05, "@1", namespace_id=3, dedicated=True
        )
    assert result == AnimationResult("completed", 1)
    nvim.api.win_set_cursor.assert_not_called()


def test_animate_lines_finds_the_window_again_after_a_mid_line_pause() -> None:
    nvim = MagicMock()
    nvim.exec_lua.side_effect = [1004, 1009]
    signals = iter([None, None, "pause", None])
    with (
        patch("vim_ai_follower.control.check_signal", side_effect=lambda *_: next(signals, None)),
        patch("vim_ai_follower.backends.nvim._wait_while_paused", return_value="resumed"),
    ):
        _animate_lines(nvim, 7, ("ab",), 0, lambda: 0.05, "@1", namespace_id=3, dedicated=True)
    assert nvim.exec_lua.call_args_list == [
        call(_FOLLOWER_WINDOW_LUA, 7, 0, False),
        call(_FOLLOWER_WINDOW_LUA, 7, 1004, False),
    ]
    assert nvim.api.win_set_cursor.call_args_list == [call(1004, [1, 1]), call(1009, [1, 2])]


def test_an_adopted_animation_records_its_window_before_anything_can_pause() -> None:
    # T24c review m2: an adopted follower asks for its own window (-1: the
    # current one, the navigation's) before the first signal check, and then
    # only ever for that one again.
    nvim = MagicMock()
    nvim.exec_lua.return_value = 1004
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        _animate_lines(nvim, 7, ("ab",), 0, lambda: 0.05, "@1", namespace_id=3)
    assert nvim.exec_lua.call_args_list == [
        call(_FOLLOWER_WINDOW_LUA, 7, -1, True),
        call(_FOLLOWER_WINDOW_LUA, 7, 1004, True),
    ]
    assert nvim.api.win_set_cursor.call_args_list == [call(1004, [1, 1]), call(1004, [1, 2])]


def test_an_adopted_animation_with_no_lines_asks_for_no_window() -> None:
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        assert _animate_lines(
            nvim, 7, (), 0, lambda: 0.05, "@1", namespace_id=3
        ) == AnimationResult("completed", 0)
    nvim.exec_lua.assert_not_called()


def test_animate_lines_forces_the_static_default_colorscheme_before_the_typing_highlight() -> None:
    # Order matters: `:colorscheme` runs `hi clear` first, which wipes any
    # highlight group already defined (VafTypingLine here, VafWriterCue
    # elsewhere) — so background+colorscheme must run BEFORE the typing
    # highlight is defined, not after. A prior commit had this backwards;
    # reverting the reorder makes this test fail (verified 2026-09-15).
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        _animate_lines(
            nvim, 7, ("a",), 0, lambda: 0.05, "@1", namespace_id=3, base_directory=Path("/tmp/x")
        )
    cmds = [recorded_call.args[0] for recorded_call in nvim.command.call_args_list]
    assert "set background=dark" in cmds
    background_index = cmds.index("set background=dark")
    colorscheme_index = next(
        index for index, command in enumerate(cmds) if "colorscheme gruvbox" in command
    )
    highlight_index = next(
        index for index, command in enumerate(cmds) if command.startswith("highlight default")
    )
    assert background_index < colorscheme_index < highlight_index


def test_animate_lines_pace_zero_types_the_whole_line_at_once() -> None:
    # The pace-0 catch-up (des-interrupt / crash replay) must not animate: one
    # whole-line insert per line, no per-char loop.
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        _animate_lines(nvim, 7, ("x",), 4, lambda: 0.0, "@1", namespace_id=1)
    nvim.api.buf_set_lines.assert_any_call(7, 4, 4, True, [""])
    nvim.api.buf_set_text.assert_called_once_with(7, 4, 0, 4, 0, ["x"])


def test_animate_lines_checks_signal_before_each_line() -> None:
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None) as check:
        _animate_lines(
            nvim, 7, ("a", "b"), 0, lambda: 0.0, "@9", namespace_id=1, base_directory=Path("/d")
        )
    # pace 0 -> only the per-line boundary checks (no per-char loop)
    assert check.call_args_list == [call("@9", Path("/d")), call("@9", Path("/d"))]


def test_animate_lines_interrupts_before_typing_when_signal_fires_immediately() -> None:
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value="interrupt"):
        result = _animate_lines(nvim, 7, ("a", "b"), 0, lambda: 0.0, "@1", namespace_id=1)
    assert result == AnimationResult("interrupted", 0)
    nvim.api.buf_set_text.assert_not_called()


def test_animate_lines_stops_partway_when_interrupt_fires_between_lines() -> None:
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", side_effect=[None, "interrupt"]):
        result = _animate_lines(nvim, 7, ("a", "b", "c"), 0, lambda: 0.0, "@1", namespace_id=1)
    assert result == AnimationResult("interrupted", 1)
    assert nvim.api.buf_set_text.call_count == 1  # only line 0 was typed


def test_animate_lines_leaves_the_partial_line_untouched_on_a_mid_char_interrupt() -> None:
    # Interrupt detected mid-line (per-char check): Alberto does not want the
    # line "finished" for him (2026-09-16) — the untyped remainder is left
    # out of the buffer, and the line does NOT count as done (index, not
    # index + 1), so it's retyped from scratch on resume.
    nvim = MagicMock()
    # line 0 "hi" typed fully, then on line 1 "abc" type 'a', interrupt before 'b'
    signals = [None, None, None, None, None, "interrupt"]
    with patch("vim_ai_follower.control.check_signal", side_effect=signals):
        result = _animate_lines(nvim, 7, ("hi", "abc"), 0, lambda: 0.05, "@1", namespace_id=1)
    assert result == AnimationResult("interrupted", 1)
    # only "h", "i", "a" were typed — no snap of "bc"
    assert nvim.api.buf_set_text.call_args_list == [
        call(7, 0, 0, 0, 0, ["h"]),
        call(7, 0, 1, 0, 1, ["i"]),
        call(7, 1, 0, 1, 0, ["a"]),
    ]


def test_animate_lines_pause_inside_the_char_loop_then_resumes(tmp_path: Path) -> None:
    # Pause mid-line (per-char check) blocks in _wait_while_paused; the resume
    # toggle then continues typing the same line from the same character.
    nvim = MagicMock()
    with (
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=[None, "pause", "pause", None, None],
        ),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        result = _animate_lines(nvim, 7, ("ab",), 0, lambda: 0.05, "@1", namespace_id=1)
    assert result == AnimationResult("completed", 1)
    assert nvim.api.buf_set_text.call_args_list == [
        call(7, 0, 0, 0, 0, ["a"]),
        call(7, 0, 1, 0, 1, ["b"]),
    ]


def test_animate_lines_interrupt_during_a_mid_line_pause_leaves_it_untouched(
    tmp_path: Path,
) -> None:
    # Paused mid-line, then interrupted: _wait_while_paused returns False, and
    # the line is left exactly as far as it was typed (nothing here, since the
    # pause landed before the first character) — not counted as done.
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause", "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        result = _animate_lines(nvim, 7, ("ab",), 0, lambda: 0.05, "@1", namespace_id=1)
    assert result == AnimationResult("interrupted", 0)
    nvim.api.buf_set_text.assert_not_called()  # no snap; nothing was typed yet


def test_animate_lines_pace_zero_skips_empty_lines(tmp_path: Path) -> None:
    # A blank line in the pace-0 catch-up path is just the seeded "" — no
    # whole-line insert for it.
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        result = _animate_lines(nvim, 7, ("a", "", "b"), 0, lambda: 0.0, "@1", namespace_id=1)
    assert result == AnimationResult("completed", 3)
    assert nvim.api.buf_set_text.call_count == 2  # the empty line inserts nothing


def test_animate_lines_pauses_then_resumes_retyping_the_same_line(tmp_path: Path) -> None:
    # "pause" at line 1's boundary blocks in _wait_while_paused; the next poll
    # returns "pause" (the resume toggle), then line 1 is finally typed.
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause", "pause", None]),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        result = _animate_lines(nvim, 7, ("a", "b"), 0, lambda: 0.0, "@1", namespace_id=1)
    assert result == AnimationResult("completed", 2)
    assert nvim.api.buf_set_text.call_count == 2  # both lines typed, none skipped


def test_animate_lines_pause_then_interrupt_returns_interrupted(tmp_path: Path) -> None:
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.control.check_signal", side_effect=["pause", "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        result = _animate_lines(nvim, 7, ("a", "b"), 0, lambda: 0.0, "@1", namespace_id=1)
    assert result == AnimationResult("interrupted", 0)
    nvim.api.buf_set_text.assert_not_called()


def test_animate_lines_saves_the_remainder_while_paused(tmp_path: Path) -> None:
    nvim = MagicMock()
    saved: list[int] = []
    with (
        patch("vim_ai_follower.control.check_signal", side_effect=["pause", "pause", None]),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        _animate_lines(
            nvim, 7, ("a",), 0, lambda: 0.0, "@1", namespace_id=1, save_pending=saved.append
        )
    assert saved == [0]  # the crash-fallback remainder starts at the paused line


def test_animate_lines_empty_completes_without_typing() -> None:
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        result = _animate_lines(nvim, 7, (), 0, lambda: 0.0, "@1", namespace_id=1)
    assert result == AnimationResult("completed", 0)
    nvim.api.buf_set_text.assert_not_called()


# --- NvimFollower (mocked pynvim.attach) ---------------------------------


def test_is_alive_true_when_an_nvim_answers_the_probe() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    with patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers") as probe:
        assert follower.is_alive() is True
    probe.assert_called_once_with("/tmp/x.sock")


@pytest.mark.parametrize("verdict", ["absent", "garbled"])
def test_is_alive_false_when_no_nvim_answers(verdict: str) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    with patch("vim_ai_follower.backends.nvim.probe_nvim", return_value=verdict):
        assert follower.is_alive() is False


def test_a_silent_nvim_is_alive() -> None:
    # Busy in `:!make`, `system()` or a Lua loop answers nothing, exactly like
    # a SIGSTOPped nvim: alive either way, so nothing collects or replaces it.
    follower = NvimFollower(socket_path="/tmp/x.sock")
    with patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="silent"):
        assert follower.is_alive() is True


def test_connect_goes_through_the_bounded_attach() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    with patch("vim_ai_follower.backends.nvim.bounded_attach", return_value="connected") as attach:
        assert follower._connect() == "connected"
    attach.assert_called_once_with("/tmp/x.sock")


def test_show_fresh_never_edits_the_real_file_and_types_the_content(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    fresh_buffer(nvim)
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        result = follower.show_fresh("/tmp/f.py", "hello\nworld\n")
    assert result == AnimationResult("completed", 2)
    # Never `:e`/`:edit` the real file — that would flash the finished content.
    issued = [recorded_call.args[0] for recorded_call in nvim.command.call_args_list]
    assert not any(command.startswith("edit ") or command.startswith("e ") for command in issued)
    # Each line is typed at its row (pace 0 here -> whole-line inserts).
    nvim.api.buf_set_text.assert_any_call(7, 0, 0, 0, 0, ["hello"])
    nvim.api.buf_set_text.assert_any_call(7, 1, 0, 1, 0, ["world"])


def test_show_fresh_opens_a_new_tab_when_requested(tmp_path: Path) -> None:
    # The bug this plan fixes: in_new_tab was accepted but never wired to
    # anything, so a second file silently replaced the first instead of
    # getting its own tab.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    fresh_buffer(nvim)
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/b.py")
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        follower.show_fresh("/tmp/b.py", "x\n", in_new_tab=True)
    cmds = [recorded_call.args[0] for recorded_call in nvim.command.call_args_list]
    assert "tabnew" in cmds
    assert "enew" not in cmds
    # The name goes on the buffer show_fresh CREATED, by handle, and that
    # buffer is put in the window of the tab the tabnew added — never "the
    # current buffer after :tabnew", which a user autocommand can move
    # (final review of backlog-sweep-3, I1).
    assert call.api.buf_set_name(nvim.api.create_buf.return_value, "/tmp/b.py") in nvim.mock_calls
    window = nvim.api.tabpage_get_win.return_value
    added = nvim.api.tabpage_get_win.call_args.args[0]
    assert added.handle == 2
    assert call.api.win_set_buf(window, 7) in nvim.mock_calls
    nvim.api.set_current_win.assert_called_once_with(window)


def test_show_fresh_marks_animating_and_leaves_the_slot_to_its_owner(tmp_path: Path) -> None:
    # The hook that acquired the slot releases it; the driver only marks.
    from vim_ai_follower import control as owner_control

    assert owner_control.try_acquire_animating("@1", tmp_path)
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    fresh_buffer(nvim)
    seen: list[bool] = []

    def _check(window_id: str, base_directory: Path | None = None) -> None:
        from vim_ai_follower import control

        seen.append(control.is_animating("@1", tmp_path))
        return

    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=_check),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        follower.show_fresh("/tmp/f.py", "a\nb\n")
    from vim_ai_follower import control

    assert seen and all(seen)
    assert control.animating_state("@1", tmp_path) == "running"


def test_show_fresh_relocks_buffer_when_completed(tmp_path: Path) -> None:
    FollowerState.set(
        "@1", "nvim", "/tmp/x.sock", adopted=False, base_directory=tmp_path
    )  # proven dedicated
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    fresh_buffer(nvim)
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        follower.show_fresh("/tmp/f.py", "a\n")
    # unlocked around the animation, then relocked (nomodifiable) after.
    # (The first option call opts the new buffer out of swap before naming it;
    # the lock protocol starts after it.)
    locks = [
        recorded_call
        for recorded_call in nvim.api.buf_set_option.call_args_list
        if recorded_call.args[1] != "swapfile"
    ]
    assert locks[0] == call(7, "modifiable", True)
    assert nvim.api.buf_set_option.call_args_list[-1] == call(7, "modifiable", False)


def test_show_fresh_leaves_buffer_modifiable_when_interrupted(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    fresh_buffer(nvim)
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        result = follower.show_fresh("/tmp/f.py", "a\nb\n")
    assert result == AnimationResult("interrupted", 0)
    # never relocked — the user owns the buffer after an interrupt (hand-over).
    assert call(7, "modifiable", False) not in nvim.api.buf_set_option.call_args_list


def test_show_fresh_pause_saves_show_fresh_remainder_then_resumes(tmp_path: Path) -> None:
    from vim_ai_follower import control

    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    fresh_buffer(nvim)
    saved: list[tuple[tuple[str, ...], bool, str]] = []
    real_save = control.save_pending_show_fresh

    def spy_save(
        window_id: str,
        lines: tuple[str, ...],
        pace: float,
        continuation: bool = False,
        base_directory: Path | None = None,
        file_path: str = "",
        partial: str | None = None,
    ) -> None:
        saved.append((lines, continuation, file_path))
        real_save(
            window_id,
            lines,
            pace,
            continuation=continuation,
            file_path=file_path,
            partial=partial,
        )

    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        # line 0 typed, pause at line 1's boundary, resume, then line 1 typed.
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause", "pause", None]),
        patch("vim_ai_follower.control.save_pending_show_fresh", side_effect=spy_save),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
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
    shows_the_buffer(nvim)
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.api.buf_line_count.return_value = 1
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    saved: list[int] = []
    real_save = control.save_pending_apply_edit

    def spy_save(
        window_id: str,
        ops: list[EditOp],
        pace: float,
        base_directory: Path | None = None,
        file_path: str = "",
        partial: str | None = None,
    ) -> None:
        saved.append(len(ops))
        real_save(window_id, ops, pace, file_path=file_path, partial=partial)

    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        # pause at op0's boundary, resume, then op0 runs (delete + line "a").
        patch("vim_ai_follower.control.check_signal", side_effect=["pause", "pause", None, None]),
        patch("vim_ai_follower.control.save_pending_apply_edit", side_effect=spy_save),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [operation])
    assert result == AnimationResult("completed", 1)
    assert saved == [1]  # remainder = the whole op list from the paused op
    nvim.api.buf_set_text.assert_called_once()


def test_apply_edit_pause_at_op_boundary_then_interrupt(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    shows_the_buffer(nvim)
    nvim.api.get_current_buf.return_value.handle = 7
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=["pause", "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [operation])
    assert result == AnimationResult("interrupted", 0)
    nvim.api.buf_set_text.assert_not_called()  # interrupted before op0's delete


def test_apply_edit_pause_inside_an_ops_lines_saves_and_resumes(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    shows_the_buffer(nvim)
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.api.buf_line_count.return_value = 1
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a", "b"))
    saved: list[int] = []

    def spy_save(
        window_id: str,
        ops: list[EditOp],
        pace: float,
        base_directory: Path | None = None,
        file_path: str = "",
        partial: str | None = None,
    ) -> None:
        saved.append(len(ops))

    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        # op boundary, line0 typed, pause at line1, resume, line1 typed.
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=[None, None, "pause", "pause", None],
        ),
        patch("vim_ai_follower.control.save_pending_apply_edit", side_effect=spy_save),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [operation])
    assert result == AnimationResult("completed", 1)
    assert saved == [1]  # the op-granular remainder is saved mid-op too
    assert nvim.api.buf_set_text.call_count == 2  # both lines eventually typed


def test_drive_keeps_a_pause_pressed_after_the_hook_announced() -> None:
    # BACKLOG D1 on this backend: the hook claims the slot ("running") before
    # it connects, finds the buffer and unlocks it; a P pressed in between was
    # wiped by _drive's own clear_signals before the first character.
    from vim_ai_follower import control

    assert control.try_acquire_animating("@1")  # the hook's announce
    control.request_pause("@1")
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.api.buf_get_changedtick.return_value = 1
    seen: list[str | None] = []

    def run() -> AnimationResult:
        seen.append(control.check_signal("@1"))  # what the driver's first check sees
        return AnimationResult("completed", 1)

    follower._drive(nvim, 7, run, None)
    assert seen == ["pause"]


def test_drive_runs_every_request_of_the_animation_without_the_deadline() -> None:
    # A hit-enter prompt in an adopted nvim mid-animation must hold the
    # typing, not cut it at RPC_TIMEOUT_SECONDS (review I2 of T16): the
    # unlock, the run, the stamp and the relock all see the deadline lifted.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    bounds = {"lifted": 0}
    nvim._session._vaf_bounds = bounds
    lifted_at: list[tuple[str, int]] = []
    nvim.api.buf_set_option.side_effect = lambda *_args: lifted_at.append(
        ("buf_set_option", bounds["lifted"])
    )
    nvim.exec_lua.side_effect = lambda *_args: lifted_at.append(("exec_lua", bounds["lifted"]))

    def run() -> AnimationResult:
        lifted_at.append(("run", bounds["lifted"]))
        return AnimationResult("completed", 1)

    with patch.object(NvimFollower, "_locks_buffers", return_value=True):
        follower._drive(nvim, 7, run, ["x"])
    assert lifted_at == [
        ("buf_set_option", 1),
        ("run", 1),
        ("exec_lua", 1),
        ("buf_set_option", 1),
    ]
    assert bounds["lifted"] == 0


def test_drive_skips_relock_for_an_adopted_follower(tmp_path: Path) -> None:
    from vim_ai_follower import state

    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    fresh_buffer(nvim)
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=True)
        follower.show_fresh("/tmp/f.py", "a\n")
    # Unlocked for the animation, but an adopted (user-owned) nvim is never
    # relocked afterwards.
    # (The first option call opts the new buffer out of swap before naming it;
    # the lock protocol starts after it.)
    locks = [
        recorded_call
        for recorded_call in nvim.api.buf_set_option.call_args_list
        if recorded_call.args[1] != "swapfile"
    ]
    assert locks[0] == call(7, "modifiable", True)
    assert call(7, "modifiable", False) not in nvim.api.buf_set_option.call_args_list


def test_drive_relocks_a_dedicated_follower_recorded_not_adopted(tmp_path: Path) -> None:
    from vim_ai_follower import state

    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    fresh_buffer(nvim)
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=False)
        follower.show_fresh("/tmp/f.py", "a\n")
    assert nvim.api.buf_set_option.call_args_list[-1] == call(7, "modifiable", False)


def test_stop_quits_a_launched_nvim(tmp_path: Path) -> None:
    from vim_ai_follower import state

    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=False)
        follower.stop()
    nvim.command.assert_called_once_with("qall!")  # dedicated nvim quit, its split closes


def test_stop_is_a_noop_for_an_adopted_nvim(tmp_path: Path) -> None:
    from vim_ai_follower import state

    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=True)
        follower.stop()
    nvim.command.assert_not_called()  # never quit the user's own editor


def test_apply_edit_deletes_then_animates_each_op(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    shows_the_buffer(nvim)
    nvim.api.get_current_buf.return_value.handle = 7
    ops = compute_edit_script("hello\nworld\n", "hello\nvim ai follower\n")
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", ops)
    assert result == AnimationResult("completed", 1)
    # replace of line 2: delete old line then type the new one (pace 0 -> whole line).
    nvim.api.buf_set_lines.assert_any_call(7, 1, 2, True, [])
    nvim.api.buf_set_text.assert_any_call(7, 1, 0, 1, 0, ["vim ai follower"])


def test_apply_edit_navigates_to_the_tab_showing_file_path(tmp_path: Path) -> None:
    # Regression guard: apply_edit used to operate on whatever tab happened
    # to be active instead of switching to file_path's own tab first, unlike
    # goto_file/close_tab/reload_and_relock/resume, which all do. Harmless
    # with a single tab, but live content corruption once multiple real
    # tabs exist: editing file A while file B's tab is active would type
    # A's diff into B's buffer, leaving A untouched.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.api.buf_line_count.return_value = 1
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        follower.apply_edit("/tmp/f.py", [operation])
    goto_file.assert_called_once_with("/tmp/f.py")


def test_apply_edit_interrupted_reports_completed_op_index(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    shows_the_buffer(nvim)
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.api.buf_line_count.return_value = 1
    ops = [
        EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",)),
        EditOp(kind="insert", start_line=3, end_line=2, new_lines=("b",)),
    ]
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        # op0: boundary + line0 between + line0 char0 (types "a"); then op1's
        # boundary interrupts -> completed op index 1.
        patch("vim_ai_follower.control.check_signal", side_effect=[None, None, None, "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", ops)
    assert result == AnimationResult("interrupted", 1)


def test_apply_edit_interrupted_inside_an_ops_line_animation(tmp_path: Path) -> None:
    # The signal fires WITHIN _animate_lines for op0's second line (not at an
    # op boundary), so apply_edit must surface interrupted at that op's index.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    shows_the_buffer(nvim)
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.api.buf_line_count.return_value = 1
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a", "b"))
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        # op boundary (None), line0 (None, typed), line1 boundary -> interrupt.
        patch("vim_ai_follower.control.check_signal", side_effect=[None, None, "interrupt"]),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [operation])
    assert result == AnimationResult("interrupted", 0)
    assert nvim.api.buf_set_text.call_count == 0  # no snap; line0 not typed at all


def test_apply_edit_pure_delete_op_needs_no_animation(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    shows_the_buffer(nvim)
    nvim.api.get_current_buf.return_value.handle = 7
    operation = EditOp(kind="delete", start_line=2, end_line=3, new_lines=())
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        result = follower.apply_edit("/tmp/f.py", [operation])
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
    with patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock", speed="lento")
        assert follower._pace_provider() == config_mod.pace_seconds_for("lento")


def test_goto_line_sets_cursor() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.api.buf_line_count.return_value = 80
    with patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim):
        follower.goto_line(3)
    assert nvim.current.window.cursor == (3, 0)


def test_close_tab_wipes_the_buffer_found_by_the_buffer_list_walk() -> None:
    FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=False)  # proven dedicated
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.exec_lua.return_value = 9
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        follower.close_tab("/tmp/f.py")
    goto_file.assert_called_once_with("/tmp/f.py")
    nvim.exec_lua.assert_called_once_with(_FIND_BUFFER_LUA, "/tmp/f.py")
    nvim.command.assert_called_once_with("silent! bwipeout! 9")


def test_close_tab_skips_the_wipe_when_no_buffer_holds_the_file() -> None:
    FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=False)  # proven dedicated
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    nvim = MagicMock()
    nvim.exec_lua.return_value = -1
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        follower.close_tab("/tmp/f.py")
    goto_file.assert_called_once_with("/tmp/f.py")
    nvim.command.assert_not_called()


def test_goto_file_switches_to_the_tab_already_showing_the_buffer() -> None:
    # A single window per tab is a special case of the general multi-window
    # loop goto_file runs (see the split-window test below).
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.exec_lua.return_value = 9
    tab_other, tab_target = MagicMock(), MagicMock()
    win_other, win_target = MagicMock(), MagicMock()
    other_buffer, target_buffer = MagicMock(number=3), MagicMock(number=9)
    nvim.api.list_tabpages.return_value = [tab_other, tab_target]
    nvim.api.tabpage_list_wins.side_effect = lambda tabpage: (
        [win_target] if tabpage is tab_target else [win_other]
    )
    nvim.api.win_get_buf.side_effect = lambda window: (
        target_buffer if window is win_target else other_buffer
    )
    with patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim):
        follower.goto_file("/tmp/f.py")
    nvim.api.set_current_win.assert_called_once_with(win_target)
    nvim.command.assert_not_called()
    nvim.api.create_buf.assert_not_called()


def test_goto_file_finds_the_buffer_in_a_split_window_not_just_the_focused_one() -> None:
    # A buffer can be visible in an unfocused split of some tab (normal in
    # an adopted nvim, the user's own editor) — checking only each tab's
    # focused window misses it and opens a duplicate tab for the same file.
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.exec_lua.return_value = 9
    tab1, tab2 = MagicMock(), MagicMock()
    win1, win2a, win2b = MagicMock(), MagicMock(), MagicMock()
    first_buffer, second_tab_first_buffer, second_tab_second_buffer = (
        MagicMock(number=1),
        MagicMock(number=3),
        MagicMock(number=9),
    )
    nvim.api.list_tabpages.return_value = [tab1, tab2]
    nvim.api.tabpage_list_wins.side_effect = lambda tabpage: (
        [win1] if tabpage is tab1 else [win2a, win2b]
    )
    window_to_buffer = {
        win1: first_buffer,
        win2a: second_tab_first_buffer,
        win2b: second_tab_second_buffer,
    }
    nvim.api.win_get_buf.side_effect = lambda window: window_to_buffer[window]
    with patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim):
        follower.goto_file("/tmp/f.py")
    nvim.api.set_current_win.assert_called_once_with(win2b)
    nvim.command.assert_not_called()


def test_goto_file_opens_a_new_tab_when_the_buffer_exists_but_isnt_shown() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.exec_lua.return_value = 9
    window = tabnew_adds_a_tab(nvim)  # no tab shows it yet
    with patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim):
        follower.goto_file("/tmp/f.py")
    nvim.command.assert_called_once_with("tabnew")
    # Into the window of the tab the tabnew ADDED, and marked there: window 0
    # is the user's own when an autocommand jumps on the tabnew (N3).
    nvim.api.win_set_buf.assert_called_once_with(window, 9)
    nvim.api.win_set_var.assert_called_once_with(window, "vaf_opened", 1)
    nvim.api.create_buf.assert_not_called()


def test_goto_file_creates_a_new_buffer_in_a_new_tab_when_none_exists() -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    window = tabnew_adds_a_tab(nvim)
    created = MagicMock(handle=42)
    nvim.api.create_buf.return_value = created
    with patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim):
        follower.goto_file("/tmp/f.py")
    nvim.command.assert_called_once_with("tabnew")
    nvim.api.create_buf.assert_called_once_with(True, False)
    nvim.api.buf_set_name.assert_called_once_with(created, "/tmp/f.py")
    nvim.api.win_set_buf.assert_called_once_with(window, 42)
    # Read in and cleared, so a plain `:w` saves it (C2-C5 (c)); after it is
    # shown, so the read lands on the new buffer and nowhere else.
    calls = nvim.mock_calls
    assert calls.index(call.api.win_set_buf(window, 42)) < calls.index(
        call.exec_lua(_READ_THEN_CLEAR_LUA, 42)
    )


def test_ensure_showing_delegates_to_goto_file_when_the_buffer_exists() -> None:
    # ensure_showing only delegates when there IS a buffer; with none it
    # loads the real disk content instead (tests/test_nvim_missing_buffer.py).
    follower = NvimFollower(socket_path="/tmp/x.sock")
    nvim = MagicMock()
    nvim.exec_lua.return_value = 9
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch.object(NvimFollower, "goto_file") as goto_file,
    ):
        follower.ensure_showing("/tmp/f.py")
    goto_file.assert_called_once_with("/tmp/f.py")


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


@pytest.mark.parametrize(
    ("content", "bomb"), [(b"\xef\xbb\xbfx = 1\n", True), (b"x = 1\n", False), (b"", False)]
)
def test_format_from_disk_takes_bomb_from_the_files_first_bytes(
    tmp_path: Path, content: bytes, bomb: bool
) -> None:
    target = tmp_path / "a.py"
    target.write_bytes(content)
    nvim = MagicMock()
    nvim.api.buf_get_option.return_value = "unix"
    _format_from_disk(nvim, 7, str(target))
    nvim.api.buf_set_option.assert_called_once_with(7, "bomb", bomb)


@pytest.mark.parametrize(
    ("content", "fileformat"),
    [
        (b"a\r\nb\r\n", "dos"),
        (b"a\r\nb", "dos"),
        (b"\xef\xbb\xbfa\r\n", "dos"),
        (b"a\nb\n", "unix"),
        (b"a\rb\n", "unix"),
        (b"a\r\nb\n", None),
        (b"a", None),
        (b"", None),
    ],
)
def test_fileformat_of(content: bytes, fileformat: str | None) -> None:
    assert _fileformat_of(content) == fileformat


@pytest.mark.parametrize("current", ["unix", "dos"])
def test_format_from_disk_sets_fileformat_only_when_it_changes(
    tmp_path: Path, current: str
) -> None:
    target = tmp_path / "a.py"
    target.write_bytes(b"a\r\nb\r\n")
    nvim = MagicMock()
    nvim.api.buf_get_option.return_value = current
    _format_from_disk(nvim, 7, str(target))
    calls = [call.args for call in nvim.api.buf_set_option.call_args_list]
    assert ((7, "fileformat", "dos") in calls) is (current == "unix")
    assert (7, "bomb", False) in calls


def test_format_from_disk_leaves_a_mixed_files_fileformat_alone(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_bytes(b"a\r\nb\n")
    nvim = MagicMock()
    _format_from_disk(nvim, 7, str(target))
    nvim.api.buf_set_option.assert_called_once_with(7, "bomb", False)


def test_format_from_disk_leaves_both_alone_when_the_file_cannot_be_read(tmp_path: Path) -> None:
    nvim = MagicMock()
    _format_from_disk(nvim, 7, str(tmp_path / "gone.py"))
    nvim.api.buf_set_option.assert_not_called()


@pytest.mark.parametrize(("lines", "restored"), [(["a", "b"], True), (["a", "OTHER"], False)])
def test_format_from_disk_puts_back_unmodified_only_on_a_buffer_holding_the_file(
    tmp_path: Path, lines: list[str], restored: bool
) -> None:
    """T12+T13 review M3: an untyped buffer whose format followed the file
    is the file again; one whose lines are not stays modified."""
    target = tmp_path / "a.py"
    target.write_bytes(b"a\r\nb\r\n")
    nvim = MagicMock()
    nvim.api.buf_get_option.side_effect = lambda buffer, name: {
        "modified": False,
        "fileformat": "unix",
    }[name]
    nvim.api.buf_get_lines.return_value = lines
    _format_from_disk(nvim, 7, str(target))
    calls = [call.args for call in nvim.api.buf_set_option.call_args_list]
    assert (7, "fileformat", "dos") in calls
    assert ((7, "modified", False) in calls) is restored


def test_animate_lines_sends_nothing_after_a_stop_in_a_mid_line_pause(tmp_path: Path) -> None:
    # Final review C1: the stop tears this nvim down, so not even the
    # typing extmark is removed.
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause"]),
        patch("vim_ai_follower.backends.nvim._wait_while_paused", return_value="stopped"),
    ):
        result = _animate_lines(nvim, 7, ("ab",), 0, lambda: 0.05, "@1", namespace_id=1)
    assert result == AnimationResult("stopped", 0)
    nvim.api.buf_set_text.assert_not_called()
    nvim.api.buf_del_extmark.assert_not_called()


def test_animate_lines_ends_stopped_on_a_stop_in_a_line_boundary_pause(tmp_path: Path) -> None:
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause"]),
        patch("vim_ai_follower.backends.nvim._wait_while_paused", return_value="stopped"),
    ):
        result = _animate_lines(nvim, 7, ("a", "b"), 0, lambda: 0.0, "@1", namespace_id=1)
    assert result == AnimationResult("stopped", 1)
    assert nvim.api.buf_set_text.call_count == 1


def test_a_public_call_closes_the_connection_it_opened() -> None:
    # pynvim leaves an unclosed connection to the garbage collector: an
    # unclosed-transport ResourceWarning in whatever runs next.
    by_thread: list[tuple[bool, MagicMock]] = []

    def connect(_socket_path: str) -> MagicMock:
        import threading

        nvim = MagicMock()
        nvim.api.buf_line_count.return_value = 10
        by_thread.append((threading.current_thread() is threading.main_thread(), nvim))
        return nvim

    with patch("vim_ai_follower.backends.nvim_connect.open_session", side_effect=connect):
        NvimFollower(socket_path="/tmp/x.sock", window_id="@1").goto_line(3)
    main = [nvim for on_main, nvim in by_thread if on_main]
    assert main
    for nvim in main:
        nvim.close.assert_called_once_with()
