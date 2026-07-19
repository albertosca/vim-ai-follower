from __future__ import annotations

from pathlib import Path

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import control  # noqa: E402
from vim_ai_follower.animate import AnimationResult  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.diff import compute_edit_script  # noqa: E402


@pytest.mark.integration
def test_show_fresh_types_the_content_into_the_named_buffer(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    result = follower.show_fresh("/tmp/x.py", "def f():\n    return 1\n")
    assert result == AnimationResult("completed", 2)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["def f():", "    return 1"]
    # never loaded from disk (the real file need not even exist)
    assert nvim.current.buffer.name.endswith("/tmp/x.py")


@pytest.mark.integration
def test_apply_edit_animates_a_line_change(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    follower.show_fresh("/tmp/x.py", "def f():\n    return 1\n")
    ops = compute_edit_script("def f():\n    return 1\n", "def f():\n    return 2\n")
    result = follower.apply_edit("/tmp/x.py", ops)
    assert result == AnimationResult("completed", 1)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["def f():", "    return 2"]


@pytest.mark.integration
def test_apply_edit_inserts_and_deletes_lines(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    before = "a\nb\nc\n"
    after = "a\nB1\nB2\n"  # replace b with two lines, drop c
    follower.show_fresh("/tmp/x.py", before)
    ops = compute_edit_script(before, after)
    result = follower.apply_edit("/tmp/x.py", ops)
    assert result == AnimationResult("completed", len(ops))

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["a", "B1", "B2"]


@pytest.mark.integration
def test_pause_stops_the_animation_then_resumes_to_the_full_content(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    lines = [f"line {i}" for i in range(5)]
    content = "\n".join(lines) + "\n"

    # Deterministic pause at line 2's boundary (3rd signal check), resumed on
    # the very next poll — the hook WAITS while paused and only returns once
    # the run has truly finished, exactly like the vim pause integration test.
    calls = {"n": 0}

    def _pause_then_resume(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] in (3, 4):
            return "pause"
        return None

    monkeypatch.setattr(control, "check_signal", _pause_then_resume)
    result = follower.show_fresh("/tmp/p.py", content)
    assert result == AnimationResult("completed", 5)
    # The crash fallback written during the wait is disarmed on resume.
    assert control.has_pending_animation("@1", cache_dir) is False

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == lines  # every line landed, none skipped


@pytest.mark.integration
def test_interrupt_leaves_the_buffer_modifiable_for_hand_over(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    calls = {"n": 0}

    def _interrupt_midway(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "interrupt" if calls["n"] == 2 else None

    monkeypatch.setattr(control, "check_signal", _interrupt_midway)
    result = follower.show_fresh("/tmp/i.py", "a\nb\nc\n")
    assert result.outcome == "interrupted"

    nvim = pynvim.attach("socket", path=headless_nvim)
    # Hand-over: an interrupted animation is never relocked, so the user owns
    # the buffer and can finish it themselves.
    assert nvim.api.buf_get_option(nvim.current.buffer.handle, "modifiable") is True
