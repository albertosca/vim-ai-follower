"""Real-nvim coverage for _run_ops' interrupt rollback: after an interrupt
inside op k the buffer the user takes over must equal
apply_ops(before, ops[:k]) — exactly the partial hooks.py computes from
completed_count and quotes back to Claude. Before the rollback the op's old
range was already deleted while k said the op never happened, so the
notification over-reported the buffer by every line the op had removed.

Uses the shared headless_nvim/tmp_path fixtures from tests/conftest.py (no
copies) and builds every expectation with diff.compute_edit_script/apply_ops
rather than hand-written line lists, so the test measures the invariant
instead of restating it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import control  # noqa: E402
from vim_ai_follower.animate import AnimationResult  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.control import PendingApplyEdit  # noqa: E402
from vim_ai_follower.diff import apply_ops, compute_edit_script  # noqa: E402


def _interrupt_at(nth: int) -> Any:
    """A check_signal double firing a single 'interrupt' on the nth call
    (1-indexed) — mirrors the helper of the same name in
    test_nvim_integration.py and test_nvim_integration_interrupt_partial_line.py
    (not imported: those modules are edited by other agents in parallel, per
    the backlog contract)."""
    calls = {"n": 0}

    def _signal(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "interrupt" if calls["n"] == nth else None

    return _signal


def _follower(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pace: float
) -> NvimFollower:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    return NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=pace)


@pytest.mark.integration
def test_interrupt_mid_line_inside_an_op_restores_the_ops_deleted_range(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two ops (bottom-to-top: "d"->"D" first, then "b"->"BBBB"); the interrupt
    # lands mid-line inside the SECOND one, so completed_count is 1 and the
    # expected buffer still carries op 1's untouched "b".
    follower = _follower(headless_nvim, tmp_path, monkeypatch, 0.01)
    before = "a\nb\nc\nd\ne\n"
    after = "a\nBBBB\nc\nD\ne\n"
    follower.show_fresh("/tmp/x.py", before)
    ops = compute_edit_script(before, after)
    assert len(ops) == 2

    # op0 boundary + op0 line boundary + "D" -> 3 calls; op1 boundary + op1
    # line boundary + "B" -> 3 more; the 7th call (char 1 of "BBBB") fires.
    monkeypatch.setattr(control, "check_signal", _interrupt_at(7))
    result = follower.apply_edit("/tmp/x.py", ops)
    assert result == AnimationResult("interrupted", 1)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == apply_ops(before, ops[: result.completed_count]).splitlines()


@pytest.mark.integration
def test_interrupt_inside_a_whole_buffer_wiping_op_restores_every_line(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The op's delete empties the buffer down to nvim's implicit blank line;
    # the rollback must bring back exactly the original lines with no
    # stranded blank at the bottom.
    follower = _follower(headless_nvim, tmp_path, monkeypatch, 0.01)
    before = "a\nb\nc\n"
    after = "XYZ\nPQ\n"
    follower.show_fresh("/tmp/x.py", before)
    ops = compute_edit_script(before, after)
    assert len(ops) == 1
    assert ops[0].start_line == 1 and ops[0].end_line == 3  # wipes the buffer

    # op boundary + line boundary + "X" -> 3 calls; the 4th (char 1) fires.
    monkeypatch.setattr(control, "check_signal", _interrupt_at(4))
    result = follower.apply_edit("/tmp/x.py", ops)
    assert result == AnimationResult("interrupted", 0)

    nvim = pynvim.attach("socket", path=headless_nvim)
    expected = apply_ops(before, ops[: result.completed_count]).splitlines()
    assert nvim.current.buffer[:] == expected  # ["a", "b", "c"], no trailing ""


@pytest.mark.integration
def test_interrupt_exactly_at_an_op_boundary_leaves_the_buffer_untouched(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Control: the op-loop's own signal check runs BEFORE the op's delete, so
    # op 1 never started and there is nothing to roll back. pace 0.0 keeps
    # every line atomic, pinning the signal call that lands on the boundary.
    follower = _follower(headless_nvim, tmp_path, monkeypatch, 0.0)
    before = "a\nb\nc\nd\ne\n"
    after = "a\nB\nc\nD\ne\n"
    follower.show_fresh("/tmp/x.py", before)
    ops = compute_edit_script(before, after)
    assert len(ops) == 2

    # op0 boundary + op0 line boundary (pace 0 types the line whole, no
    # per-char checks) -> 2 calls; the 3rd is op1's boundary.
    monkeypatch.setattr(control, "check_signal", _interrupt_at(3))
    result = follower.apply_edit("/tmp/x.py", ops)
    assert result == AnimationResult("interrupted", 1)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == apply_ops(before, ops[: result.completed_count]).splitlines()


@pytest.mark.integration
def test_des_interrupt_after_a_rolled_back_interrupt_lands_at_the_full_content(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole hand-off cycle hooks.py drives: interrupt, rewrite_buffer to
    # the computed partial, resume the remainder. The rollback makes the
    # first step a no-op in content terms (the buffer already IS the partial)
    # — which is the point — and the replay must still converge on `after`.
    follower = _follower(headless_nvim, tmp_path, monkeypatch, 0.01)
    before = "a\nb\nc\nd\ne\n"
    after = "a\nBBBB\nc\nD\ne\n"
    follower.show_fresh("/tmp/x.py", before)
    ops = compute_edit_script(before, after)

    monkeypatch.setattr(control, "check_signal", _interrupt_at(7))
    result = follower.apply_edit("/tmp/x.py", ops)
    assert result == AnimationResult("interrupted", 1)

    partial = apply_ops(before, ops[: result.completed_count])
    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == partial.splitlines()

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    assert follower.rewrite_buffer("/tmp/x.py", partial).outcome == "completed"
    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == partial.splitlines()

    pending = PendingApplyEdit(
        ops=ops[result.completed_count :], pace_seconds=0.0, file_path="/tmp/x.py"
    )
    assert follower.resume(pending).outcome == "completed"
    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == after.splitlines()


@pytest.mark.integration
def test_des_interrupt_after_a_rolled_back_wiping_op_lands_at_the_full_content(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same cycle for the wiping op, where a rollback that left the implicit
    # blank behind would push an extra line into the replayed result.
    follower = _follower(headless_nvim, tmp_path, monkeypatch, 0.01)
    before = "a\nb\nc\n"
    after = "XYZ\nPQ\n"
    follower.show_fresh("/tmp/x.py", before)
    ops = compute_edit_script(before, after)

    monkeypatch.setattr(control, "check_signal", _interrupt_at(4))
    result = follower.apply_edit("/tmp/x.py", ops)
    assert result == AnimationResult("interrupted", 0)

    partial = apply_ops(before, ops[: result.completed_count])
    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    assert follower.rewrite_buffer("/tmp/x.py", partial).outcome == "completed"

    pending = PendingApplyEdit(
        ops=ops[result.completed_count :], pace_seconds=0.0, file_path="/tmp/x.py"
    )
    assert follower.resume(pending).outcome == "completed"
    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == after.splitlines()
