from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import control  # noqa: E402
from vim_ai_follower.animate import AnimationResult  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.diff import compute_edit_script  # noqa: E402
from vim_ai_follower.status_surface import NvimStatusSurface  # noqa: E402


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


@pytest.mark.integration
def test_goto_file_navigates_between_buffers_and_close_tab_evicts(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    nvim = pynvim.attach("socket", path=headless_nvim)

    file_a = str(tmp_path / "a.py")
    file_b = str(tmp_path / "b.py")

    follower.show_fresh(file_a, "a = 1\n")
    follower.show_fresh(file_b, "b = 1\n")

    def _buf_names() -> list[str]:
        return [nvim.api.buf_get_name(b) for b in nvim.api.list_bufs()]

    names = _buf_names()
    assert file_a in names
    assert file_b in names
    assert nvim.api.buf_get_name(nvim.api.get_current_buf()) == file_b

    follower.goto_file(file_a)
    assert nvim.api.buf_get_name(nvim.api.get_current_buf()) == file_a

    follower.close_tab(file_b)
    assert file_b not in _buf_names()


def _floating_windows(nvim: Any) -> list[int]:
    return [w for w in nvim.api.list_wins() if nvim.api.win_get_config(w)["relative"] != ""]


@pytest.mark.integration
def test_set_writer_opens_a_floating_window_with_the_label_then_clear_closes_it(
    headless_nvim: str,
) -> None:
    surface = NvimStatusSurface(socket_path=headless_nvim)

    surface.set_writer("code-reviewer", "colour78")

    nvim = pynvim.attach("socket", path=headless_nvim)
    floating = _floating_windows(nvim)
    assert len(floating) >= 1
    buf = nvim.api.win_get_buf(floating[0])
    lines = nvim.api.buf_get_lines(buf, 0, -1, True)
    assert any("code-reviewer" in line for line in lines)

    surface.set_state("Claude waiting — :w releases · S discards")
    lines_with_state = nvim.api.buf_get_lines(buf, 0, -1, True)
    assert any("Claude waiting" in line for line in lines_with_state)
    # still exactly one floating status window, not a second one stacked on top
    assert len(_floating_windows(nvim)) == 1

    surface.clear()
    assert _floating_windows(pynvim.attach("socket", path=headless_nvim)) == []


@pytest.mark.integration
def test_goto_file_finds_the_buffer_despite_a_symlinked_path_component(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression for the Task 6 review finding: nvim CANONICALIZES buffer
    # names (macOS resolves /tmp -> /private/tmp), so a hand-rolled
    # nvim_list_bufs + raw string compare misses an existing buffer whenever
    # a path component is a symlink — the followup buf_set_name then raises
    # E95 (buffer with that resolved name already exists). Deliberately use
    # a literal /tmp/... path here (NOT the tmp_path fixture, which pytest
    # pre-resolves and would hide the bug) to exercise the real symlink.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    nvim = pynvim.attach("socket", path=headless_nvim)

    file_a = f"/tmp/vaf-symlink-regress-{uuid.uuid4().hex}.py"

    follower.show_fresh(file_a, "a = 1\n")
    buf_after_show = nvim.api.get_current_buf()
    canonical_name = nvim.api.buf_get_name(buf_after_show)

    # Must not raise E95, and must land on the SAME buffer already showing
    # file_a (by canonical name) rather than creating a duplicate.
    follower.goto_file(file_a)
    assert nvim.api.get_current_buf() == buf_after_show

    matching = [b for b in nvim.api.list_bufs() if nvim.api.buf_get_name(b) == canonical_name]
    assert len(matching) == 1


@pytest.mark.integration
def test_touch_and_evict_wipes_the_evicted_buffer_on_real_nvim(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end proof of the generic eviction path (hooks._touch_and_evict)
    # against a real headless nvim: with max_tabs=1, editing file B (after A
    # was shown) evicts A's buffer entirely.
    from vim_ai_follower import cache, hooks
    from vim_ai_follower.state import FollowerState

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    nvim = pynvim.attach("socket", path=headless_nvim)

    file_a = str(tmp_path / "a.py")
    file_b = str(tmp_path / "b.py")

    follower.show_fresh(file_a, "a = 1\n")
    FollowerState.set(
        "@1", "nvim", headless_nvim, current_file=file_a, open_files=(file_a,), shown_any=True
    )
    current = FollowerState.read("@1")
    assert current is not None

    hooks._touch_and_evict("@1", follower, current, file_b, max_tabs=1)
    follower.show_fresh(file_b, "b = 1\n")

    names = [nvim.api.buf_get_name(b) for b in nvim.api.list_bufs()]
    assert file_a not in names
    assert file_b in names
    refreshed = FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.open_files == (file_b,)
