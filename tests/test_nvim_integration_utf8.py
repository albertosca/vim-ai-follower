"""Real-nvim coverage for multi-byte (non-ASCII) content in the nvim backend.

Alberto writes Portuguese all day, so accented characters and an em dash go
through the per-character typing loop constantly. nvim's API columns are BYTE
offsets while the loop walks code points, and before the fix the two diverged
on the first multi-byte character: a plain animation of `alpha — beta` landed
in the real buffer as b'alpha \\xe2 beta\\x80\\x94' (found by the visual-battery
QA run, 2026-09-21, reproduced here).

The buffer is compared BYTE-for-byte, not by the decoded string: pynvim
decodes with surrogateescape, so a corrupted buffer still yields a Python str
and a naive equality check can be read past. `_encoded` forces the comparison
back onto bytes and blows up loudly on a lone surrogate.

These run at pace > 0 on purpose — pace <= 0 inserts each line whole at column
0, which is byte-safe by construction and can never see the bug.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import control  # noqa: E402
from vim_ai_follower.animate import AnimationResult  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.control import PendingShowFresh  # noqa: E402
from vim_ai_follower.diff import compute_edit_script  # noqa: E402

# One character from each UTF-8 width class the typing loop can meet: a
# 2-byte accented letter, a 3-byte em dash, a 4-byte astral emoji, plus 3-byte
# CJK. Any of them desynchronises a code-point column from a byte column.
_PORTUGUESE = "A ação — café e ideéia"
_EMOJI = "\U0001f600 pronto"
_CJK = "中文测试"


def _encoded(lines: list[str]) -> list[bytes]:
    """The buffer lines as the raw bytes nvim holds.

    pynvim hands back str decoded with surrogateescape, so a split character
    survives as lone surrogates instead of raising. Re-encoding with the same
    handler recovers the real bytes and keeps the failure message readable.
    """
    return [line.encode("utf-8", "surrogateescape") for line in lines]


def _expected(lines: list[str]) -> list[bytes]:
    return [line.encode("utf-8") for line in lines]


def _buffer_lines(socket_path: str) -> list[str]:
    nvim = pynvim.attach("socket", path=socket_path)
    return list(nvim.api.buf_get_lines(nvim.api.get_current_buf().handle, 0, -1, True))


def _interrupt_at(nth: int) -> Any:
    """A check_signal double firing a single 'interrupt' on the nth call
    (1-indexed) — mirrors the helper in the other nvim integration modules
    (deliberately copied, not imported: those files are owned by other
    tasks)."""
    calls = {"n": 0}

    def _signal(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "interrupt" if calls["n"] == nth else None

    return _signal


@pytest.mark.integration
def test_show_fresh_types_multi_byte_content_byte_identically(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.001)
    lines = [_PORTUGUESE, _EMOJI, _CJK, "plain ascii"]
    content = "".join(line + "\n" for line in lines)

    result = follower.show_fresh(str(tmp_path / "acentos.py"), content)
    assert result == AnimationResult("completed", 4)

    assert _encoded(_buffer_lines(headless_nvim)) == _expected(lines)


@pytest.mark.integration
def test_apply_edit_replaces_a_line_with_a_multi_byte_one_byte_identically(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.001)
    file_a = str(tmp_path / "edit.py")
    before = "primeira\nsegunda\nterceira\n"
    after = f"primeira\n{_PORTUGUESE}\nterceira\n"

    follower.show_fresh(file_a, before)
    ops = compute_edit_script(before, after)
    result = follower.apply_edit(file_a, ops)
    assert result == AnimationResult("completed", len(ops))

    assert _encoded(_buffer_lines(headless_nvim)) == _expected(after.splitlines())


@pytest.mark.integration
def test_interrupt_after_a_multi_byte_character_leaves_a_decodable_prefix(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Line 1 is "ação"; stop right after its 3rd character so the row
    # holds exactly "açã" — whole characters, never half of one. The
    # stop is deliberately one character PAST the first multi-byte one: a stop
    # right after "ç" lands at identical columns in both currencies and
    # would pass against the bug. Then the full des-interrupt sequence
    # (rewrite_buffer to the fully-typed prefix, then resume the stored
    # remainder) must converge byte-for-byte.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.01)
    file_a = str(tmp_path / "interrompe.py")
    lines = ["ok", "ação", "fim"]
    content = "".join(line + "\n" for line in lines)

    # calls: line0 boundary(1) + "o"(2) + "k"(3); line1 boundary(4) +
    # "a"(5) + "ç"(6) + "ã"(7); the 8th call, before "o", interrupts.
    monkeypatch.setattr(control, "check_signal", _interrupt_at(8))
    result = follower.show_fresh(file_a, content)
    assert result == AnimationResult("interrupted", 1)

    # "" is show_fresh's own seed blank, never consumed because the retype
    # stopped before reaching it.
    assert _encoded(_buffer_lines(headless_nvim)) == _expected(["ok", "açã", ""])

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    partial = "\n".join(lines[:1])
    assert follower.rewrite_buffer(file_a, partial).outcome == "completed"
    pending = PendingShowFresh(
        lines=tuple(lines[1:]), pace_seconds=0.0, continuation=True, file_path=file_a
    )
    assert follower.resume(pending).outcome == "completed"

    assert _encoded(_buffer_lines(headless_nvim)) == _expected(lines)


@pytest.mark.integration
def test_pause_across_a_multi_byte_character_converges_byte_identically(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pause with the byte offset already ahead of the character index, resume
    # on the next poll: the offset must survive the wait, so the rest of the
    # line lands where it belongs.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.01)
    lines = [_PORTUGUESE, _CJK]
    content = "".join(line + "\n" for line in lines)

    # calls: line0 boundary(1) + "A"(2) + " "(3) + "a"(4); pause on the 5th
    # and 6th (the trigger, then the toggle read as resume) — right after
    # "ç" has gone in and the byte offset has overtaken the char index.
    calls = {"n": 0}

    def _pause_then_resume(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "pause" if calls["n"] in (6, 7) else None

    monkeypatch.setattr(control, "check_signal", _pause_then_resume)
    result = follower.show_fresh(str(tmp_path / "pausa.py"), content)
    assert result == AnimationResult("completed", 2)

    assert _encoded(_buffer_lines(headless_nvim)) == _expected(lines)
