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
from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower.animate import AnimationResult
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
        base_dir: Path | None = None,
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
        base_dir: Path | None = None,
        file_path: str = "",
        partial: str | None = None,
    ) -> None:
        saved.append(partial)

    return spy


def test_show_fresh_pause_records_the_fully_typed_prefix_as_the_partial(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.current.buffer.handle = 7
    saved: list[tuple[str | None, bool]] = []

    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        # line 0 typed, pause at line 1's boundary, resume, line 1 typed.
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause", "pause", None]),
        patch("vim_ai_follower.control.save_pending_show_fresh", _fresh_partial_spy(saved)),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
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
    nvim.current.buffer.handle = 7
    saved: list[tuple[str | None, bool]] = []

    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        # line0 boundary, "a"; line1 boundary, "b"; pause before "c"; resume.
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=[None, None, None, None, "pause", "pause", None],
        ),
        patch("vim_ai_follower.control.save_pending_show_fresh", _fresh_partial_spy(saved)),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        assert follower.show_fresh("/tmp/f.py", "a\nbc\n") == AnimationResult("completed", 2)

    assert saved == [("a\n", True)]  # "b" was typed but "bc" is not complete


def test_run_ops_pause_records_the_applied_prefix_as_the_partial(tmp_path: Path) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.funcs.bufnr.return_value = 3
    nvim.api.buf_get_lines.return_value = ["a", "b", "c"]
    nvim.api.buf_line_count.return_value = 3
    # Bottom-to-top, as compute_edit_script emits them: line 3 first.
    ops = [
        EditOp(kind="replace", start_line=3, end_line=3, new_lines=("C",)),
        EditOp(kind="replace", start_line=1, end_line=1, new_lines=("A",)),
    ]
    saved: list[str | None] = []

    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        # op0 boundary + its one line; pause at op1's boundary; resume; op1.
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=[None, None, "pause", "pause", None, None],
        ),
        patch("vim_ai_follower.control.save_pending_apply_edit", _ops_partial_spy(saved)),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
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
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.funcs.bufnr.return_value = 3
    nvim.api.buf_get_lines.return_value = ["a", "x"]  # seedless: exactly the partial
    saved: list[tuple[str | None, bool]] = []
    pending = PendingShowFresh(
        lines=("b", "c"), pace_seconds=0.0, continuation=True, file_path="/tmp/f.py"
    )

    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause", "pause", None]),
        patch("vim_ai_follower.control.save_pending_show_fresh", _fresh_partial_spy(saved)),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
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
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.funcs.bufnr.return_value = 3
    nvim.api.buf_get_lines.return_value = ["a", ""]
    saved: list[tuple[str | None, bool]] = []
    pending = PendingShowFresh(
        lines=("b", "c"), pace_seconds=0.0, continuation=True, file_path="/tmp/f.py"
    )

    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "pause", "pause", None]),
        patch("vim_ai_follower.control.save_pending_show_fresh", _fresh_partial_spy(saved)),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        assert follower.resume(pending, seeded=True) == AnimationResult("completed", 2)

    assert saved == [("a\nb\n", True)]
