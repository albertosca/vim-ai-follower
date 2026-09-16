"""The tmux animation drivers' crash-fallback `partial`.

run_lines/run_ops send keystrokes and can never read the screen back, so the
`partial` they persist on a pause has to be computed from a base the caller
hands them. These tests pin the EXACT string that reaches
control.save_pending_show_fresh / save_pending_apply_edit at a chosen pause
point — a boundary pause and a mid-line/mid-op pause for each driver, since the
partly-typed unit is rolled back before the wait and must therefore contribute
nothing.

Pause points are expressed as an ordinal signal-check count, measured rather
than guessed: run_lines checks once per KeySequence (4 per line: opener, text,
Escape, Escape), run_ops likewise (op0 of the ops used below spans checks 1-10,
op1 checks 11-15).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

from vim_ai_follower import control
from vim_ai_follower.animate import AnimationResult, run_lines, run_ops
from vim_ai_follower.diff import EditOp
from vim_ai_follower.tmux import TmuxPane

# A three-line retype: 4 signal checks per line, so line k's first check is
# 4*k + 1 and its text lands just before check 4*k + 3.
_LINES = ("alpha", "beta", "gamma")

# Bottom-to-top, the order compute_edit_script emits: op0 replaces line 2 of
# "a\nb\nc\n" with two lines, op1 inserts one line at the top.
_OPS = [
    EditOp(kind="replace", start_line=2, end_line=2, new_lines=("XX", "YY")),
    EditOp(kind="insert", start_line=1, end_line=0, new_lines=("ZZ",)),
]
_BASE = "a\nb\nc\n"


def _pause_at(check: int, tmp_path: Path, seen: list[object]) -> object:
    """A check_signal double that pauses at the `check`-th call, snapshots the
    pending the wait loop just wrote on the call after that, and then resumes
    (the pause signal is a toggle: a second read means "resume")."""
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == check:
            return "pause"
        if calls["n"] == check + 1:
            seen.append(control.load_pending_animation("@1", tmp_path))
            return "pause"
        return None

    return _check


def test_run_lines_pause_at_a_line_boundary_records_the_lines_already_typed(
    tmp_path: Path,
) -> None:
    pane = cast(TmuxPane, MagicMock())
    seen: list[object] = []
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_at(9, tmp_path, seen)):
        result = run_lines(pane, "@1", _LINES, pace_seconds=0.0, base_dir=tmp_path, base_content="")
    assert result == AnimationResult("completed", 3)
    assert seen == [
        control.PendingShowFresh(("gamma",), 0.0, continuation=True, partial="alpha\nbeta\n")
    ]


def test_run_lines_pause_mid_line_leaves_the_half_typed_line_out_of_the_partial(
    tmp_path: Path,
) -> None:
    # Check 11 is line 2's text send: "gamma" is on screen when the pause
    # lands, and is then undone by the rollback. It is also the remainder's
    # first entry, so counting it in the partial too is exactly what
    # duplicated it on the nvim side — the partial must stop at "beta".
    pane = cast(TmuxPane, MagicMock())
    seen: list[object] = []
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_at(11, tmp_path, seen)):
        result = run_lines(pane, "@1", _LINES, pace_seconds=0.0, base_dir=tmp_path, base_content="")
    assert result == AnimationResult("completed", 3)
    # Proof the pause really landed mid-line and not on the boundary: "gamma"
    # was typed, undone, and retyped. Without this the test would pass just as
    # happily against a boundary pause, asserting nothing about half-typed
    # lines.
    sent = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent == ["i", "alpha", "o", "beta", "o", "gamma", "u", "o", "gamma"]
    assert seen == [
        control.PendingShowFresh(("gamma",), 0.0, continuation=True, partial="alpha\nbeta\n")
    ]


def test_run_lines_pause_before_the_first_line_records_an_empty_partial(
    tmp_path: Path,
) -> None:
    # "" is a real answer, not a missing one: nothing of the content is on
    # screen yet. It must not collapse to None, which means "not recorded".
    pane = cast(TmuxPane, MagicMock())
    seen: list[object] = []
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_at(1, tmp_path, seen)):
        result = run_lines(pane, "@1", _LINES, pace_seconds=0.0, base_dir=tmp_path, base_content="")
    assert result == AnimationResult("completed", 3)
    assert seen == [control.PendingShowFresh(_LINES, 0.0, continuation=False, partial="")]


def test_run_lines_continuation_run_prepends_the_prefix_already_on_screen(
    tmp_path: Path,
) -> None:
    # The resume case: the buffer already holds the partial this run is
    # continuing from, so the new partial is that prefix plus what this run
    # has typed — mirroring nvim's _resume_fresh, which computes
    # _terminated([*prefix, *lines[:index]]) from the buffer it can read.
    pane = cast(TmuxPane, MagicMock())
    seen: list[object] = []
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_at(5, tmp_path, seen)):
        result = run_lines(
            pane,
            "@1",
            ("beta", "gamma"),
            pace_seconds=0.0,
            base_dir=tmp_path,
            continuation=True,
            base_content="alpha\n",
        )
    assert result == AnimationResult("completed", 2)
    assert seen == [
        control.PendingShowFresh(("gamma",), 0.0, continuation=True, partial="alpha\nbeta\n")
    ]


def test_run_lines_keeps_a_trailing_blank_line_in_the_prefix(tmp_path: Path) -> None:
    # The terminated form is lossless where a "\n".join round-trip is not:
    # "alpha\n\n" must not come back as "alpha\n".
    pane = cast(TmuxPane, MagicMock())
    seen: list[object] = []
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_at(5, tmp_path, seen)):
        run_lines(
            pane,
            "@1",
            ("beta", "gamma"),
            pace_seconds=0.0,
            base_dir=tmp_path,
            continuation=True,
            base_content="alpha\n\n",
        )
    assert seen == [
        control.PendingShowFresh(("gamma",), 0.0, continuation=True, partial="alpha\n\nbeta\n")
    ]


def test_run_lines_without_a_base_records_no_partial(tmp_path: Path) -> None:
    # The fallback must stay meaningful: a caller that genuinely cannot say
    # what is on screen leaves partial=None, and the consumer keeps its
    # live-buffer behavior instead of rebuilding to something invented.
    pane = cast(TmuxPane, MagicMock())
    seen: list[object] = []
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_at(9, tmp_path, seen)):
        run_lines(pane, "@1", _LINES, pace_seconds=0.0, base_dir=tmp_path)
    assert seen == [control.PendingShowFresh(("gamma",), 0.0, continuation=True, partial=None)]


def test_run_ops_pause_at_an_op_boundary_records_the_ops_already_applied(
    tmp_path: Path,
) -> None:
    pane = cast(TmuxPane, MagicMock())
    seen: list[object] = []
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_at(11, tmp_path, seen)):
        result = run_ops(pane, "@1", _OPS, pace_seconds=0.0, base_dir=tmp_path, base_content=_BASE)
    assert result == AnimationResult("completed", 2)
    # op0 replaced "b" with "XX"/"YY"; op1 has not run yet.
    assert seen == [control.PendingApplyEdit(_OPS[1:], 0.0, partial="a\nXX\nYY\nc")]


def test_run_ops_pause_mid_op_leaves_the_rolled_back_op_out_of_the_partial(
    tmp_path: Path,
) -> None:
    # Check 13 is inside op1's insert half (its "gg"/"O" prefix is already on
    # screen). run_ops undoes both halves of the partly-typed op before
    # waiting, so the buffer is back at the op boundary and the partial must
    # still be op0's result — op-granular, exactly like the remainder.
    pane = cast(TmuxPane, MagicMock())
    seen: list[object] = []
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_at(13, tmp_path, seen)):
        result = run_ops(pane, "@1", _OPS, pace_seconds=0.0, base_dir=tmp_path, base_content=_BASE)
    assert result == AnimationResult("completed", 2)
    # Proof the pause landed INSIDE op1: its "gg"/"O" opener went out, was
    # rolled back with a "u", and the whole op was replayed afterwards.
    sent = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent == [":2d", ":1", "o", "XX", "YY", "gg", "O", "u", "gg", "O", "ZZ"]
    assert seen == [control.PendingApplyEdit(_OPS[1:], 0.0, partial="a\nXX\nYY\nc")]


def test_run_ops_pause_before_the_first_op_records_the_untouched_base(
    tmp_path: Path,
) -> None:
    pane = cast(TmuxPane, MagicMock())
    seen: list[object] = []
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_at(1, tmp_path, seen)):
        result = run_ops(pane, "@1", _OPS, pace_seconds=0.0, base_dir=tmp_path, base_content=_BASE)
    assert result == AnimationResult("completed", 2)
    assert seen == [control.PendingApplyEdit(_OPS, 0.0, partial="a\nb\nc")]


def test_run_ops_without_a_base_records_no_partial(tmp_path: Path) -> None:
    pane = cast(TmuxPane, MagicMock())
    seen: list[object] = []
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_at(11, tmp_path, seen)):
        run_ops(pane, "@1", _OPS, pace_seconds=0.0, base_dir=tmp_path)
    assert seen == [control.PendingApplyEdit(_OPS[1:], 0.0, partial=None)]
