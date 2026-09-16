"""Real-nvim proof that a fully-applied prefix ending in a BLANK line survives
the two paths that rebuild a buffer from a persisted `partial`.

Measured against a real headless nvim before the fix: an apply_edit whose
bottom hunk turns the last line into a blank one is interrupted right after
that hunk lands. The live buffer is ['one','two','three',''] — the blank line
is genuinely there — but the partial the savers persisted was
apply_ops's "\\n".join form, "one\\ntwo\\nthree\\n", which rewrite_buffer
splitlines() back into three lines. Both the des-interrupt replay and the
crash-fallback catch-up therefore rebuilt a buffer one line short and finished
one line short.

Reuses the headless_nvim fixture from conftest.py, alongside the other
test_nvim_integration_*.py modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import control, hooks  # noqa: E402
from vim_ai_follower.animate import AnimationResult  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.diff import apply_ops, compute_edit_script  # noqa: E402

_BEFORE = "one\ntwo\nthree\nfour\n"
_AFTER = "ONE\ntwo\nthree\n\n"
_PREFIX_LINES = ["one", "two", "three", ""]


def _interrupt_at(nth: int) -> Any:
    """A check_signal double firing a single 'interrupt' on the nth call
    (1-indexed). Mirrors the helper in the sibling integration modules; not
    imported, because those files are edited by other agents in parallel."""
    calls = {"n": 0}

    def _signal(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "interrupt" if calls["n"] == nth else None

    return _signal


def _interrupt_after_the_blank_line_hunk(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[NvimFollower, str, list[Any], str]:
    """Drive a real nvim to the interrupt point and return the follower, the
    file path, the ops, and the partial the savers would persist.

    Signal-check ordinal 3 is measured, not guessed (a throwaway probe walked
    1..11: 1-2 stop inside op0, 3-4 stop at the op1 boundary, 5+ complete)."""
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    file_a = str(tmp_path / "a.py")
    NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0).show_fresh(
        file_a, _BEFORE
    )
    ops = compute_edit_script(_BEFORE, _AFTER)
    assert len(ops) == 2  # not vacuous: one op would make every prefix agree

    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    monkeypatch.setattr(control, "check_signal", _interrupt_at(3))
    result = follower.apply_edit(file_a, ops)
    assert result == AnimationResult("interrupted", 1)

    nvim = pynvim.attach("socket", path=headless_nvim)
    # The ground truth this whole task is about: the blank fourth line is
    # really on screen. Anything the partial cannot express is a line lost.
    assert nvim.current.buffer[:] == _PREFIX_LINES

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    return follower, file_a, ops, apply_ops(_BEFORE, ops[: result.completed_count])


@pytest.mark.integration
def test_the_persisted_partial_reproduces_the_live_buffer_including_the_blank_line(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, partial = _interrupt_after_the_blank_line_hunk(headless_nvim, tmp_path, monkeypatch)
    # rewrite_buffer's own round trip, at the source: the shape the consumers
    # get back out has to equal the shape nvim really holds.
    assert partial.splitlines() == _PREFIX_LINES


@pytest.mark.integration
def test_des_interrupt_replay_keeps_the_blank_line_of_the_rebuilt_prefix(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    follower, file_a, ops, partial = _interrupt_after_the_blank_line_hunk(
        headless_nvim, tmp_path, monkeypatch
    )
    # Exactly what hooks._replay_remainder does with the stored pair.
    assert follower.rewrite_buffer(file_a, partial).outcome == "completed"
    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == _PREFIX_LINES

    pending = control.PendingApplyEdit(ops=ops[1:], pace_seconds=0.0, file_path=file_a)
    assert follower.resume(pending, seeded=False).outcome == "completed"

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == _AFTER.splitlines()


@pytest.mark.integration
def test_crash_fallback_catchup_keeps_the_blank_line_of_the_rebuilt_prefix(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    follower, file_a, ops, partial = _interrupt_after_the_blank_line_hunk(
        headless_nvim, tmp_path, monkeypatch
    )
    control.save_pending_apply_edit("@1", ops[1:], 0.01, file_path=file_a, partial=partial)
    pending = control.load_pending_animation("@1")
    assert pending is not None
    hooks._consume_pending_catchup(follower, file_a, pending)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == _AFTER.splitlines()
