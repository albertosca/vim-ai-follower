"""Real-nvim coverage for the crash-fallback "pace-0 catch-up": the silent
fast-forward hooks._consume_pending_catchup runs when a killed hook left a
persisted remainder behind and the NEXT edit hook reclaims the window.

Unlike the des-interrupt replay (hooks._replay_remainder), nothing had
rebuilt the buffer before this ran, so the catch-up used to trust the LIVE
buffer's shape — _resume_fresh's `start_row = len(existing) - 1` and
_run_ops's range delete both assume a clean line boundary at the tail. Since
an interrupt no longer snaps the current line to its full text, that tail is
routinely a half-typed line, and the leftover was pushed down instead of
replaced (measured against real nvim: ['ab', 'wx', 'wxyz', 'q'] for a
three-line file). These tests pin the rebuilt-from-persisted-state behavior.

pace > 0 throughout so the interrupt genuinely lands mid-character: pace <= 0
skips _animate_lines's per-char loop entirely and can only ever stop at a
line/op boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import control, hooks  # noqa: E402
from vim_ai_follower.animate import AnimationResult  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.diff import apply_ops, compute_edit_script  # noqa: E402


def _interrupt_at(nth: int) -> Any:
    """A check_signal double firing a single 'interrupt' on the nth call
    (1-indexed). Mirrors test_nvim_integration.py's helper of the same name;
    not imported, because that module is edited by other agents in parallel."""
    calls = {"n": 0}

    def _signal(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "interrupt" if calls["n"] == nth else None

    return _signal


@pytest.mark.integration
def test_catchup_after_a_mid_line_show_fresh_stop_does_not_duplicate_the_partial_line(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exact repro measured against real nvim: show_fresh interrupted 2
    # chars into line 1, then the hook process is KILLED — so the remainder
    # is persisted but nothing ever rewrites the buffer (no des-interrupt,
    # no hand-off). The next edit hook consumes the pending at pace 0.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.01)
    file_a = str(tmp_path / "a.py")
    content = "ab\nwxyz\nq\n"

    monkeypatch.setattr(control, "check_signal", _interrupt_at(7))
    result = follower.show_fresh(file_a, content)
    assert result == AnimationResult("interrupted", 1)

    nvim = pynvim.attach("socket", path=headless_nvim)
    # The half-typed tail line the killed hook leaves behind (plus show_fresh's
    # own unconsumed seed blank).
    assert nvim.current.buffer[:] == ["ab", "wx", ""]

    # Exactly what hooks._animate_edit persists on the interrupt path.
    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    control.save_pending_show_fresh(
        "@1",
        tuple(content.splitlines())[result.completed_count :],
        0.01,
        continuation=result.completed_count > 0,
        file_path=file_a,
        partial=hooks._reconstruct_partial_fresh(content, result.completed_count),
    )

    pending = control.load_pending_animation("@1")
    assert pending is not None
    hooks._consume_pending_catchup(follower, file_a, pending)

    nvim = pynvim.attach("socket", path=headless_nvim)
    # No duplicated "wx", no stray seed blank: exactly the intended content.
    assert nvim.current.buffer[:] == content.splitlines()


@pytest.mark.integration
def test_catchup_after_a_mid_op_mid_line_apply_edit_stop_lands_at_full_content(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same class, PendingApplyEdit side. The op replaces one line with THREE,
    # and the stop lands mid-line 1 of the op — so the buffer carries two
    # leftover rows while the replayed op's range delete only removes one.
    # (With a single leftover row the old code converged by coincidence,
    # which is why this case needs a multi-line op.)
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    file_a = str(tmp_path / "a.py")
    before = "a\nb\nc\n"
    after = "a\nWW\nXX\nYY\nc\n"
    NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0).show_fresh(
        file_a, before
    )
    ops = compute_edit_script(before, after)
    assert len(ops) == 1

    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.01)
    # op-boundary + line0-boundary + "W" + "W" + line1-boundary + "X" -> 6
    # calls; the 7th (before the second "X") fires the interrupt.
    monkeypatch.setattr(control, "check_signal", _interrupt_at(7))
    result = follower.apply_edit(file_a, ops)
    assert result == AnimationResult("interrupted", 0)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["a", "WW", "X", "c"]  # two leftover rows

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    control.save_pending_apply_edit(
        "@1",
        ops[result.completed_count :],
        0.01,
        file_path=file_a,
        partial=apply_ops(before, ops[: result.completed_count]),
    )

    pending = control.load_pending_animation("@1")
    assert pending is not None
    hooks._consume_pending_catchup(follower, file_a, pending)

    nvim = pynvim.attach("socket", path=headless_nvim)
    # No stranded "X" row left over from the abandoned op.
    assert nvim.current.buffer[:] == after.splitlines()


@pytest.mark.integration
def test_catchup_with_a_clean_boundary_tail_still_converges(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The previously-working case must keep working: a pace-0 show_fresh can
    # only stop at a line boundary, so the buffer's tail is the untouched
    # seed blank rather than a half-typed line.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    file_a = str(tmp_path / "a.py")
    content = "ab\nwxyz\nq\n"

    # pace 0: one check per line, so the 2nd call stops cleanly before line 1.
    monkeypatch.setattr(control, "check_signal", _interrupt_at(2))
    result = follower.show_fresh(file_a, content)
    assert result == AnimationResult("interrupted", 1)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["ab", ""]  # clean boundary + seed blank

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    control.save_pending_show_fresh(
        "@1",
        tuple(content.splitlines())[result.completed_count :],
        0.0,
        continuation=True,
        file_path=file_a,
        partial=hooks._reconstruct_partial_fresh(content, result.completed_count),
    )

    pending = control.load_pending_animation("@1")
    assert pending is not None
    hooks._consume_pending_catchup(follower, file_a, pending)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == content.splitlines()


@pytest.mark.integration
def test_catchup_from_a_legacy_pending_without_a_partial_still_converges(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Backward compatibility: a pending file written before `partial` existed
    # loads with partial=None, which means "not recorded" — the catch-up then
    # keeps trusting the live buffer, exactly as it always did. With a clean
    # boundary that still lands on the full content.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    file_a = str(tmp_path / "a.py")
    content = "ab\nwxyz\nq\n"

    monkeypatch.setattr(control, "check_signal", _interrupt_at(2))
    assert follower.show_fresh(file_a, content) == AnimationResult("interrupted", 1)

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    control.save_pending_show_fresh("@1", ("wxyz", "q"), 0.0, continuation=True, file_path=file_a)
    pending = control.load_pending_animation("@1")
    assert pending is not None
    assert pending.partial is None
    hooks._consume_pending_catchup(follower, file_a, pending)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == content.splitlines()
