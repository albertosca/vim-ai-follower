"""Real-nvim coverage for the mid-line interrupt behavior change: on interrupt,
the current line must be left exactly as far as it was typed (no snap to its
full text), and the interrupted line must not count as shown — see
backends/nvim.py's _animate_lines docstring. These tests drive a genuine
per-char animation (pace > 0) so the interrupt lands mid-character, unlike
most of test_nvim_integration.py's pace-0 fixtures, where every interrupt is
forced to a line/op boundary (pace <= 0 skips the per-char loop entirely)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import control  # noqa: E402
from vim_ai_follower.animate import AnimationResult  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.control import PendingApplyEdit, PendingShowFresh  # noqa: E402
from vim_ai_follower.diff import apply_ops, compute_edit_script  # noqa: E402


def _interrupt_at(nth: int) -> Any:
    """A check_signal double that fires a single 'interrupt' on the nth call
    (1-indexed) and None otherwise — mirrors test_nvim_integration.py's
    helper of the same name (not imported: that module is edited by other
    agents in parallel, per the backlog contract)."""
    calls = {"n": 0}

    def _signal(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "interrupt" if calls["n"] == nth else None

    return _signal


@pytest.mark.integration
def test_show_fresh_interrupt_leaves_exactly_the_typed_chars_on_the_row(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pace > 0 so the per-char loop actually runs (pace <= 0 types a whole
    # line atomically and can never be interrupted mid-character). Content:
    # line0 "ab" (2 chars, fully typed), line1 "wxyz" (interrupt after 2 of
    # its 4 chars), line2 "q" (never reached).
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.01)
    file_a = str(tmp_path / "a.py")
    content = "ab\nwxyz\nq\n"

    # Call sequence: outer(line0) + char0 + char1 [types "ab"] -> 3 calls;
    # outer(line1) + char0("w") + char1("x") -> 3 more calls; the 7th call
    # (char index 2, before typing "y") fires the interrupt.
    monkeypatch.setattr(control, "check_signal", _interrupt_at(7))
    result = follower.show_fresh(file_a, content)

    # The interrupted line (index 1, "wxyz") is NOT counted as shown: only
    # line0 was fully typed.
    assert result == AnimationResult("interrupted", 1)

    nvim = pynvim.attach("socket", path=headless_nvim)
    # Exactly the 2 typed characters on row 1, nothing after them on that
    # row (no snap of "yz"), and line2 never started. The trailing "" is
    # show_fresh's own seed blank, pushed down but never consumed since the
    # retype never reached it — same provenance as the existing pace-0
    # catch-up tests in test_nvim_integration.py.
    assert nvim.current.buffer[:] == ["ab", "wx", ""]
    # buffer stays modifiable — the user owns it after an interrupt
    assert nvim.api.buf_get_option(nvim.current.buffer.handle, "modifiable") is True


@pytest.mark.integration
def test_show_fresh_des_interrupt_after_mid_line_stop_lands_at_full_content_no_dup(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The full des-interrupt sequence hooks.py._await_user_handoff drives:
    # rewrite_buffer to the partial made of only the FULLY typed lines (the
    # interrupted line is dropped, not kept half-typed), then resume replays
    # the stored remainder — which starts with that same line, retyped from
    # scratch. Must land at exactly the original content: no duplicated line
    # (the half-typed "wx" must not survive) and nothing missing.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.01)
    file_a = str(tmp_path / "a.py")
    content = "ab\nwxyz\nq\n"
    lines = content.splitlines()

    monkeypatch.setattr(control, "check_signal", _interrupt_at(7))
    result = follower.show_fresh(file_a, content)
    assert result == AnimationResult("interrupted", 1)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["ab", "wx", ""]  # the mid-line stop, pre-des-interrupt

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    # Exactly what hooks._reconstruct_partial_fresh produces: only the fully
    # typed lines (completed_count == 1), the half-typed "wx" is discarded.
    partial = "\n".join(lines[:1])
    assert partial == "ab"
    rebuilt = follower.rewrite_buffer(file_a, partial)
    assert rebuilt.outcome == "completed"

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["ab"]  # seedless partial; "wx" is gone

    pending = PendingShowFresh(
        lines=tuple(lines[1:]), pace_seconds=0.0, continuation=True, file_path=file_a
    )
    replay = follower.resume(pending)
    assert replay.outcome == "completed"

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == lines  # exactly ["ab", "wxyz", "q"], no dup/missing
    assert nvim.api.buf_get_option(nvim.current.buffer.handle, "modifiable") is False


@pytest.mark.integration
def test_apply_edit_des_interrupt_after_mid_op_mid_line_stop_lands_at_full_content(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same guarantee, for an edit applied to an existing file: the op that was
    # mid-line when interrupted is discarded wholesale (op-level bookkeeping
    # already treats a not-fully-typed op as not completed, unaffected by
    # this change — see _run_ops), rewrite_buffer rebuilds the pre-op state,
    # and resume replays that whole op again from scratch. Since the rollback
    # landed (test_nvim_integration_rollback.py), _run_ops itself already
    # restores that pre-op state on the way out, so rewrite_buffer here is a
    # no-op in content terms rather than a repair.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.01)
    file_a = str(tmp_path / "a.py")
    before = "a\nb\nc\n"
    after = "a\nWXYZ\nQR\nc\n"
    follower.show_fresh(file_a, before)
    ops = compute_edit_script(before, after)
    assert len(ops) == 1  # single replace op: b -> WXYZ, QR

    # op-boundary check + the op's own line-boundary check + char0("W") +
    # char1("X") -> 4 calls; the 5th call (char index 2, before typing "Y")
    # fires the interrupt.
    monkeypatch.setattr(control, "check_signal", _interrupt_at(5))
    result = follower.apply_edit(file_a, ops)
    assert result == AnimationResult("interrupted", 0)  # op0 not counted as done

    nvim = pynvim.attach("socket", path=headless_nvim)
    # The half-typed "WX" and the row holding it are gone and the "b" the op
    # had deleted is back: _run_ops rolls an interrupted op back to its clean
    # boundary, so the buffer matches what completed_count == 0 claims.
    assert nvim.current.buffer[:] == ["a", "b", "c"]

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    partial = apply_ops(before, ops[: result.completed_count])
    assert partial == "a\nb\nc"  # op0 hadn't completed: nothing of it survives
    rebuilt = follower.rewrite_buffer(file_a, partial)
    assert rebuilt.outcome == "completed"

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["a", "b", "c"]  # back to the pre-op state, no "WX"

    pending = PendingApplyEdit(
        ops=ops[result.completed_count :], pace_seconds=0.0, file_path=file_a
    )
    replay = follower.resume(pending)
    assert replay.outcome == "completed"

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == after.splitlines()  # ["a", "WXYZ", "QR", "c"]
