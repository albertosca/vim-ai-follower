"""The nvim backend's own crash-fallback savers must record the partial.

A hook killed while PAUSED leaves a remainder persisted by the backend's
save_pending callbacks, not by hooks._animate_edit — and the buffer it leaves
behind is mid-animation, routinely ending on a half-typed line. Without a
recorded partial the later pace-0 catch-up falls back to trusting that shape
and duplicates the leftover (measured against real nvim: ['ab','wx','wxyz',
'q'] where ['ab','wxyz','q'] was intended). Mocked pynvim, pattern from
test_nvim.py."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from helpers import fresh_buffer, route_exec_lua, shows_the_buffer

from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends import nvim as nvim_module
from vim_ai_follower.backends.nvim import NvimFollower
from vim_ai_follower.control import PendingShowFresh
from vim_ai_follower.diff import EditOp


@pytest.fixture(autouse=True)
def _no_sleep() -> Iterator[None]:
    with patch("vim_ai_follower.backends.nvim.time.sleep"):
        yield


def _fresh_partial_spy(saved: list[tuple[str | None, bool]]) -> object:
    def spy(
        window_id: str,
        lines: tuple[str, ...],
        pace: float,
        continuation: bool = False,
        base_directory: Path | None = None,
        file_path: str = "",
        partial: str | None = None,
    ) -> None:
        saved.append((partial, continuation))

    return spy


def _ops_partial_spy(saved: list[str | None]) -> object:
    def spy(
        window_id: str,
        ops: list[EditOp],
        pace: float,
        base_directory: Path | None = None,
        file_path: str = "",
        partial: str | None = None,
    ) -> None:
        saved.append(partial)

    return spy


def test_show_fresh_pause_records_the_fully_typed_prefix_as_the_partial(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    fresh_buffer(nvim)
    saved: list[tuple[str | None, bool]] = []

    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        # line 0 typed, pause at line 1's boundary, resume, line 1 typed.
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause", "pause", None]),
        patch("vim_ai_follower.control.save_pending_show_fresh", _fresh_partial_spy(saved)),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        assert follower.show_fresh("/tmp/f.py", "a\nb\n") == AnimationResult("completed", 2)

    # show_fresh wipes the buffer first, so the partial is exactly the lines
    # already typed — in the terminated-newline form hooks._terminated uses.
    assert saved == [("a\n", True)]


def test_show_fresh_mid_char_pause_leaves_the_half_typed_line_out_of_the_partial(
    tmp_path: Path,
) -> None:
    # The half-typed line is the FIRST entry of the remainder (it gets retyped
    # from scratch), so it must NOT also appear in the partial — that double
    # count is exactly what duplicated the line.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.05)
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name="/tmp/f.py")
    fresh_buffer(nvim)
    saved: list[tuple[str | None, bool]] = []

    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        # line0 boundary, "a"; line1 boundary, "b"; pause before "c"; resume.
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=[None, None, None, None, "pause", "pause", None],
        ),
        patch("vim_ai_follower.control.save_pending_show_fresh", _fresh_partial_spy(saved)),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        assert follower.show_fresh("/tmp/f.py", "a\nbc\n") == AnimationResult("completed", 2)

    assert saved == [("a\n", True)]  # "b" was typed but "bc" is not complete


def test_run_ops_pause_records_the_applied_prefix_as_the_partial(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    shows_the_buffer(nvim)
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.exec_lua.return_value = 3
    nvim.api.buf_get_lines.return_value = ["a", "b", "c"]
    nvim.api.buf_line_count.return_value = 3
    # Bottom-to-top, as compute_edit_script emits them: line 3 first.
    ops = [
        EditOp(kind="replace", start_line=3, end_line=3, new_lines=("C",)),
        EditOp(kind="replace", start_line=1, end_line=1, new_lines=("A",)),
    ]
    saved: list[str | None] = []

    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        # op0 boundary + its one line; pause at op1's boundary; resume; op1.
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=[None, None, "pause", "pause", None, None],
        ),
        patch("vim_ai_follower.control.save_pending_apply_edit", _ops_partial_spy(saved)),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        assert follower.apply_edit("/tmp/f.py", ops) == AnimationResult("completed", 2)

    # op0 (line 3 -> "C") had landed; op1 had not.
    assert saved == ["a\nb\nC\n"]


def test_resume_fresh_pause_records_the_buffer_prefix_plus_the_typed_lines(
    tmp_path: Path,
) -> None:
    # A des-interrupt replay that is itself paused: the partial must include
    # the lines already in the buffer before the replay started, not just the
    # ones this run typed.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    shows_the_buffer(nvim)
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.exec_lua.return_value = 3
    nvim.api.buf_get_lines.return_value = ["a", "x"]  # seedless: exactly the partial
    saved: list[tuple[str | None, bool]] = []
    pending = PendingShowFresh(
        lines=("b", "c"), pace_seconds=0.0, continuation=True, file_path="/tmp/f.py"
    )

    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause", "pause", None]),
        patch("vim_ai_follower.control.save_pending_show_fresh", _fresh_partial_spy(saved)),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        assert follower.resume(pending) == AnimationResult("completed", 2)

    assert saved == [("a\nx\nb\n", True)]


def test_resume_fresh_pause_excludes_the_trailing_seed_blank_from_the_partial(
    tmp_path: Path,
) -> None:
    # seeded=True: the last buffer row is show_fresh's unconsumed seed blank,
    # which the replay types in FRONT of — it is not part of the partial.
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    shows_the_buffer(nvim)
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.exec_lua.return_value = 3
    nvim.api.buf_get_lines.return_value = ["a", ""]
    saved: list[tuple[str | None, bool]] = []
    pending = PendingShowFresh(
        lines=("b", "c"), pace_seconds=0.0, continuation=True, file_path="/tmp/f.py"
    )

    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause", "pause", None]),
        patch("vim_ai_follower.control.save_pending_show_fresh", _fresh_partial_spy(saved)),
        patch("vim_ai_follower.cache.CACHE_DIRECTORY", tmp_path),
    ):
        assert follower.resume(pending, seeded=True) == AnimationResult("completed", 2)

    assert saved == [("a\nb\n", True)]


def test_reload_from_disk_is_the_des_interrupt_reload() -> None:
    """The catch-up grounding reuses reload_and_relock's `edit!` and its
    adopted-never-locked rule rather than a second copy of them."""
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    with patch.object(NvimFollower, "reload_and_relock") as reload:
        assert follower.reload_from_disk("/tmp/f.py", unless_changed_since_probe=False)
    reload.assert_called_once_with("/tmp/f.py")


@pytest.mark.parametrize("unchanged", [True, False])
def test_an_adopted_reload_from_disk_runs_only_while_the_probed_tick_holds(
    unchanged: bool,
) -> None:
    """One request compares the changedtick the probe recorded and runs
    `edit!` (backlog-sweep-4 T14); an adopted nvim is never locked."""
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.exec_lua.return_value = unchanged
    with (
        patch.object(NvimFollower, "_connect", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
        patch.object(NvimFollower, "_locks_buffers", return_value=False),
    ):
        assert follower.reload_from_disk("/tmp/f.py", unless_changed_since_probe=True) is unchanged
    assert nvim.exec_lua.call_args_list == [call(nvim_module._RELOAD_IF_UNCHANGED_LUA, 7)]
    assert call.api.buf_set_option(7, "modifiable", False) not in nvim.mock_calls


def test_a_dedicated_reload_from_disk_is_locked_again() -> None:
    """The reload compared against the probed tick is the adopted one, but a
    caller asking it of a dedicated follower (a stop and start between the
    hook's state read and the reload) gets the dedicated lock back: a
    dedicated buffer is never left modifiable."""
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.exec_lua.return_value = True
    with (
        patch.object(NvimFollower, "_connect", return_value=nvim),
        patch.object(NvimFollower, "goto_file"),
        patch.object(NvimFollower, "_locks_buffers", return_value=True),
    ):
        assert follower.reload_from_disk("/tmp/f.py", unless_changed_since_probe=True)
    assert call.api.buf_set_option(7, "modifiable", False) in nvim.mock_calls
