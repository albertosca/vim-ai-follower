from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import control  # noqa: E402
from vim_ai_follower.animate import AnimationResult  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.control import PendingApplyEdit, PendingShowFresh  # noqa: E402
from vim_ai_follower.diff import apply_ops, compute_edit_script  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402
from vim_ai_follower.status_surface import NvimStatusSurface  # noqa: E402


def _interrupt_at(nth: int) -> Any:
    """A check_signal double that fires a single 'interrupt' on the nth call
    (1-indexed) and None otherwise — the deterministic mid-animation stop the
    resume/hand-over tests below drive."""
    calls = {"n": 0}

    def _signal(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "interrupt" if calls["n"] == nth else None

    return _signal


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


def _tab_buffer_names(nvim: Any) -> list[str]:
    names = []
    for tabpage in nvim.api.list_tabpages():
        win = nvim.api.tabpage_get_win(tabpage)
        buf = nvim.api.win_get_buf(win)
        names.append(nvim.api.buf_get_name(buf))
    return names


@pytest.mark.integration
def test_multiple_fresh_files_open_as_separate_tabs(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    follower.show_fresh("/tmp/a.py", "a = 1\n")
    follower.show_fresh("/tmp/b.py", "b = 2\n", in_new_tab=True)

    nvim = pynvim.attach("socket", path=headless_nvim)
    names = _tab_buffer_names(nvim)
    assert len(names) == 2
    assert any(name.endswith("/tmp/a.py") for name in names)
    assert any(name.endswith("/tmp/b.py") for name in names)
    # the tab for the most recently shown file is the active one
    assert nvim.current.buffer.name.endswith("/tmp/b.py")


@pytest.mark.integration
def test_editing_an_earlier_file_switches_back_to_its_tab(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    follower.show_fresh("/tmp/a.py", "a = 1\nb = 1\n")
    follower.show_fresh("/tmp/b.py", "b = 2\n", in_new_tab=True)

    ops = compute_edit_script("a = 1\nb = 1\n", "a = 2\nb = 1\n")
    follower.apply_edit("/tmp/a.py", ops)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert len(nvim.api.list_tabpages()) == 2
    assert nvim.current.buffer.name.endswith("/tmp/a.py")
    assert nvim.current.buffer[:] == ["a = 2", "b = 1"]


@pytest.mark.integration
def test_close_tab_removes_the_tab_not_just_the_buffer(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    follower.show_fresh("/tmp/a.py", "a = 1\n")
    follower.show_fresh("/tmp/b.py", "b = 2\n", in_new_tab=True)
    follower.close_tab("/tmp/a.py")

    nvim = pynvim.attach("socket", path=headless_nvim)
    names = _tab_buffer_names(nvim)
    assert len(names) == 1
    assert names[0].endswith("/tmp/b.py")


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
def test_stop_quits_a_launched_nvim(
    headless_nvim: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait_until: Any,
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    # Recorded not adopted -> stop() quits the dedicated nvim (its split closes).
    FollowerState.set("@1", "nvim", headless_nvim, adopted=False)
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1")
    assert follower.is_alive()

    follower.stop()

    assert wait_until(lambda: not follower.is_alive(), timeout=5.0)


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
    # Anchored top-right, clear of the top-left where the retype types and
    # parks the cursor (2026-07-20 live-smoke occlusion fix): col must be
    # offset from 0, not sitting on top of the animated content.
    assert nvim.api.win_get_config(floating[0])["col"] > 0
    # The identity is the (colored) window title; the body shows the activity.
    title = nvim.api.win_get_config(floating[0])["title"]
    assert any("code-reviewer" in chunk[0] for chunk in title)
    buf = nvim.api.win_get_buf(floating[0])
    lines = nvim.api.buf_get_lines(buf, 0, -1, True)
    # Identity only — no activity line: "Writing..." here outlived the
    # animation and lied (2026-08-25 battery Check 5 finding).
    assert not any("Writing..." in line for line in lines)

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


@pytest.mark.integration
def test_reload_and_relock_reloads_disk_and_relocks_a_launched_follower(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Des-interrupt with NO pending: discard the user's unsaved typing by
    # reloading the file Claude wrote (edit!), then relock read-only. A
    # launched (non-adopted) follower IS relocked.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    file_a = str(tmp_path / "a.py")
    Path(file_a).write_text("final = 1\n")
    FollowerState.set(
        "@1", "nvim", headless_nvim, open_files=(file_a,), shown_any=True, adopted=False
    )
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    follower.show_fresh(file_a, "typed = 999\n")

    nvim = pynvim.attach("socket", path=headless_nvim)
    buf = nvim.api.get_current_buf().handle
    nvim.api.buf_set_option(buf, "modifiable", True)
    nvim.api.buf_set_lines(buf, 0, -1, True, ["user junk in progress"])

    follower.reload_and_relock(file_a)

    nvim = pynvim.attach("socket", path=headless_nvim)
    # the user's typing is gone; the buffer holds the finished disk content
    assert nvim.current.buffer[:] == ["final = 1"]
    assert nvim.api.buf_get_option(nvim.current.buffer.handle, "modifiable") is False


@pytest.mark.integration
def test_reload_and_relock_leaves_an_adopted_follower_modifiable(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    file_a = str(tmp_path / "a.py")
    Path(file_a).write_text("final = 1\n")
    FollowerState.set(
        "@1", "nvim", headless_nvim, open_files=(file_a,), shown_any=True, adopted=True
    )
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    follower.show_fresh(file_a, "typed = 999\n")

    follower.reload_and_relock(file_a)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["final = 1"]
    # an adopted nvim is the user's own editor: never locked out of it
    assert nvim.api.buf_get_option(nvim.current.buffer.handle, "modifiable") is True


@pytest.mark.integration
def test_des_interrupt_replays_a_show_fresh_remainder_to_exact_content(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Entry point A (des-interrupt): rewrite_buffer rebuilds the interrupt-point
    # buffer (seedless, exactly the partial), then resume appends the remaining
    # whole lines to EXACTLY the final content — no stray seed blank, no
    # dropped line.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    file_a = str(tmp_path / "a.py")
    content = "l0\nl1\nl2\nl3\n"
    lines = content.splitlines()

    monkeypatch.setattr(control, "check_signal", _interrupt_at(3))  # stop before l2
    result = follower.show_fresh(file_a, content)
    assert result == AnimationResult("interrupted", 2)

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    partial = "\n".join(lines[:2])
    rebuilt = follower.rewrite_buffer(file_a, partial)
    assert rebuilt.outcome == "completed"
    pending = PendingShowFresh(
        lines=tuple(lines[2:]), pace_seconds=0.0, continuation=True, file_path=file_a
    )
    replay = follower.resume(pending)
    assert replay.outcome == "completed"

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == lines
    # relocked read-only for a launched follower once the replay completed
    assert nvim.api.buf_get_option(nvim.current.buffer.handle, "modifiable") is False


@pytest.mark.integration
def test_des_interrupt_replays_an_apply_edit_remainder_to_exact_content(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    file_a = str(tmp_path / "a.py")
    before = "a\nb\nc\n"
    after = "a\nB1\nB2\nB3\n"
    follower.show_fresh(file_a, before)
    ops = compute_edit_script(before, after)

    monkeypatch.setattr(control, "check_signal", _interrupt_at(2))
    result = follower.apply_edit(file_a, ops)
    assert result.outcome == "interrupted"

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    partial = apply_ops(before, ops[: result.completed_count])
    rebuilt = follower.rewrite_buffer(file_a, partial)
    assert rebuilt.outcome == "completed"
    pending = PendingApplyEdit(
        ops=ops[result.completed_count :], pace_seconds=0.0, file_path=file_a
    )
    replay = follower.resume(pending)
    assert replay.outcome == "completed"

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == after.splitlines()


@pytest.mark.integration
def test_pace0_consume_of_an_interrupted_show_fresh_lands_at_full_content(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Entry point B (the hooks.py:439 pace-0 consume): resume runs directly on
    # the LIVE interrupted buffer, which still carries the trailing seed blank
    # show_fresh only drops on a completed outcome. The resume must strip that
    # seed and still land at EXACTLY the full content.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    file_a = str(tmp_path / "a.py")
    content = "l0\nl1\nl2\nl3\n"
    lines = content.splitlines()

    monkeypatch.setattr(control, "check_signal", _interrupt_at(3))
    assert follower.show_fresh(file_a, content) == AnimationResult("interrupted", 2)

    nvim = pynvim.attach("socket", path=headless_nvim)
    # the interrupt left a trailing seed blank at the bottom (the crux)
    assert nvim.current.buffer[:] == ["l0", "l1", ""]

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    pending = PendingShowFresh(
        lines=tuple(lines[2:]), pace_seconds=0.0, continuation=True, file_path=file_a
    )
    # entry B: the live buffer still carries show_fresh's trailing seed blank,
    # so provenance is passed explicitly (seeded=True) rather than sniffed.
    follower.resume(pending, seeded=True)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == lines  # seed dropped, nothing lost


@pytest.mark.integration
def test_des_interrupt_replays_a_remainder_after_consecutive_trailing_blanks(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Entry point A with a partial that ends in TWO consecutive blank lines —
    # the exact shape the "\n".join round-trip used to collapse. The lossless
    # _reconstruct_partial_fresh + explicit seeded=False must land at EXACTLY
    # the full content with both blanks preserved.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    file_a = str(tmp_path / "a.py")
    content = "a\n\n\nb\nc\n"
    lines = content.splitlines()
    assert lines == ["a", "", "", "b", "c"]

    monkeypatch.setattr(control, "check_signal", _interrupt_at(4))  # stop before "b"
    result = follower.show_fresh(file_a, content)
    assert result == AnimationResult("interrupted", 3)  # a + the two blanks typed

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    # Exactly what hooks._reconstruct_partial_fresh produces for the hook, then
    # rewrite_buffer rebuilds the seedless interrupt-point buffer from it.
    partial = "".join(f"{ln}\n" for ln in lines[:3])
    assert partial == "a\n\n\n"
    rebuilt = follower.rewrite_buffer(file_a, partial)
    assert rebuilt.outcome == "completed"

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["a", "", ""]  # seedless partial, blanks intact

    pending = PendingShowFresh(
        lines=tuple(lines[3:]), pace_seconds=0.0, continuation=True, file_path=file_a
    )
    replay = follower.resume(pending, seeded=False)
    assert replay.outcome == "completed"

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["a", "", "", "b", "c"]  # both blanks survive


@pytest.mark.integration
def test_pace0_consume_after_consecutive_trailing_blanks_lands_at_full_content(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Entry point B (pace-0 consume) with a partial ending in two blank lines.
    # The live interrupted buffer carries the trailing seed blank on TOP of the
    # two content blanks; seeded=True must drop only the seed and preserve both
    # content blanks.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    file_a = str(tmp_path / "a.py")
    content = "a\n\n\nb\nc\n"
    lines = content.splitlines()

    monkeypatch.setattr(control, "check_signal", _interrupt_at(4))
    assert follower.show_fresh(file_a, content) == AnimationResult("interrupted", 3)

    nvim = pynvim.attach("socket", path=headless_nvim)
    # a + two content blanks + the trailing seed blank show_fresh has not
    # dropped yet (four lines, the last being the seed).
    assert nvim.current.buffer[:] == ["a", "", "", ""]

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    pending = PendingShowFresh(
        lines=tuple(lines[3:]), pace_seconds=0.0, continuation=True, file_path=file_a
    )
    follower.resume(pending, seeded=True)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["a", "", "", "b", "c"]  # seed dropped, blanks kept


@pytest.mark.integration
def test_resume_from_scratch_types_the_whole_file_when_nothing_was_shown(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # k == 0 (interrupt before the first line): the buffer is a lone seed
    # blank; resume must type every line into it and land exactly at content,
    # with no leading or trailing stray blank.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    file_a = str(tmp_path / "a.py")
    content = "x\ny\nz\n"
    lines = content.splitlines()

    monkeypatch.setattr(control, "check_signal", _interrupt_at(1))  # nothing shown
    assert follower.show_fresh(file_a, content) == AnimationResult("interrupted", 0)

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    pending = PendingShowFresh(
        lines=tuple(lines), pace_seconds=0.0, continuation=False, file_path=file_a
    )
    # entry B on a lone seed buffer ([""]): seeded=True types into it and drops
    # the seed on completion.
    follower.resume(pending, seeded=True)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == lines


@pytest.mark.integration
def test_hand_over_reasserts_the_buffer_is_modifiable(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    file_a = str(tmp_path / "a.py")
    follower.show_fresh(file_a, "a = 1\n")  # completes -> relocked nomodifiable
    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.api.buf_get_option(nvim.current.buffer.handle, "modifiable") is False

    follower.hand_over()

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.api.buf_get_option(nvim.current.buffer.handle, "modifiable") is True


@pytest.mark.integration
def test_resume_persists_a_remainder_when_paused_mid_replay(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A des-interrupt replay that is itself PAUSED must persist the still-
    # remaining lines (crash fallback) while it waits, exactly like the
    # first-pass animation. _wait_while_paused discards that file again on
    # exit, so spy on the save to prove the fallback path ran with the
    # correct op-granular remainder.
    from vim_ai_follower import cache

    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cache, "CACHE_DIR", cache_dir)
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    file_a = str(tmp_path / "a.py")
    follower.show_fresh(file_a, "p\n")  # a one-line prefix already on screen

    saved: list[tuple[str, ...]] = []
    real_save = control.save_pending_show_fresh

    def _spy_save(window_id: str, lines: tuple[str, ...], *args: Any, **kwargs: Any) -> None:
        saved.append(tuple(lines))
        real_save(window_id, lines, *args, **kwargs)

    monkeypatch.setattr(control, "save_pending_show_fresh", _spy_save)

    # Pause on resume's 2nd signal check (after the first replayed line "q"),
    # then interrupt out of the wait.
    calls = {"n": 0}

    def _pause_then_interrupt(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == 2:
            return "pause"
        if calls["n"] >= 3:
            return "interrupt"
        return None

    monkeypatch.setattr(control, "check_signal", _pause_then_interrupt)
    pending = PendingShowFresh(
        lines=("q", "r", "s"), pace_seconds=0.05, continuation=True, file_path=file_a
    )
    result = follower.resume(pending)
    assert result.outcome == "interrupted"
    # the remainder past the first replayed line ("q") was persisted
    assert saved == [("r", "s")]


@pytest.mark.integration
def test_standalone_hook_post_edit_animates_a_headless_nvim_end_to_end(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Task 7 (nvim-standalone-no-tmux plan): a real (no-tmux) edit hook must
    # resolve the standalone session id, find the follower registered under
    # it, and drive a real headless nvim end to end — no TMUX_PANE anywhere.
    from vim_ai_follower import cache, hooks

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    window_id = "term-qa"  # session._standalone_id("qa") -> "term-qa"
    FollowerState.set(window_id, "nvim", headless_nvim, origin="", shown_any=False)

    target = tmp_path / "standalone.py"
    target.write_text("def f():\n    return 1\n")
    env = {"TERM_SESSION_ID": "qa"}  # deliberately no TMUX_PANE
    payload: dict[str, Any] = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target)},
    }

    assert hooks.cmd_hook_post(env, payload) == 0

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["def f():", "    return 1"]
    assert nvim.current.buffer.name.endswith("/standalone.py")


@pytest.mark.integration
def test_resume_without_a_file_path_operates_on_the_current_buffer(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Legacy/unknown pending state carries an empty file_path: resume must skip
    # the goto_file re-select and replay onto whatever buffer is current.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    file_a = str(tmp_path / "a.py")
    follower.show_fresh(file_a, "one\n")  # current buffer, one line, no seed

    pending = PendingShowFresh(
        lines=("two", "three"), pace_seconds=0.0, continuation=True, file_path=""
    )
    result = follower.resume(pending)
    assert result.outcome == "completed"

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["one", "two", "three"]


@pytest.mark.integration
def test_the_float_comes_back_after_its_tab_is_closed_under_it(headless_nvim: str) -> None:
    """The float window dies with its tab (eviction past max_tabs closes the
    tab it was opened in), but a scratch buffer only HIDES on window close —
    it survived under the name "vaf-status", so every later buf_set_name hit
    E95 inside suppress() and the cue never came back for the life of that
    nvim (whole-branch review, 2026-09-23; clear() was just one of the ways
    to strand it)."""
    surface = NvimStatusSurface(socket_path=headless_nvim)
    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.command("tabnew")
    surface.set_state("Writing...")
    assert len(_floating_windows(nvim)) == 1

    nvim.command("tabclose")
    assert _floating_windows(nvim) == []
    # Wiped WITH its window, not merely hidden — checked here, before the
    # next cue, because _ensure_window's stale-buffer sweep would otherwise
    # clean up after a hidden one and mask it.
    names = [nvim.api.buf_get_name(b) for b in nvim.api.list_bufs()]
    assert not any(name.endswith("vaf-status") for name in names), names

    surface.set_state("Writing...")
    floating = _floating_windows(nvim)
    assert len(floating) == 1, "the cue never came back after its tab closed"
    lines = nvim.api.buf_get_lines(nvim.api.win_get_buf(floating[0]), 0, -1, True)
    assert any("Writing..." in line for line in lines)


@pytest.mark.integration
def test_a_leftover_status_buffer_does_not_block_the_float(headless_nvim: str) -> None:
    """A long-lived nvim can already hold a hidden "vaf-status" buffer from
    an older version of this code (which only hid it). Naming a fresh one
    over it would hit E95 inside suppress() and the cue would never show."""
    nvim = pynvim.attach("socket", path=headless_nvim)
    stale = nvim.api.create_buf(False, True)
    nvim.api.buf_set_name(stale, "vaf-status")

    NvimStatusSurface(socket_path=headless_nvim).set_state("Writing...")

    floating = _floating_windows(nvim)
    assert len(floating) == 1, "a leftover vaf-status buffer blocked the float"
    lines = nvim.api.buf_get_lines(nvim.api.win_get_buf(floating[0]), 0, -1, True)
    assert any("Writing..." in line for line in lines)


@pytest.mark.integration
def test_the_status_float_follows_the_animation_to_the_current_tab(headless_nvim: str) -> None:
    """A writer float opened in tab 1 used to receive "Writing..." while the
    animation ran in tab 2, where nobody could see it: _find_window walked
    list_wins() across ALL tabs. The float must move to the current tab and
    keep the writer's title — and there must never be two of them."""
    surface = NvimStatusSurface(socket_path=headless_nvim)
    nvim = pynvim.attach("socket", path=headless_nvim)
    surface.set_writer("code-reviewer", "colour78")
    nvim.command("tabnew")

    surface.set_state("Writing...")

    current = [
        w
        for w in nvim.api.tabpage_list_wins(nvim.api.get_current_tabpage())
        if nvim.api.win_get_config(w)["relative"] != ""
    ]
    assert len(current) == 1, "no status float on the tab the animation runs in"
    config = nvim.api.win_get_config(current[0])
    assert any("code-reviewer" in chunk[0] for chunk in config["title"])
    assert "FloatBorder:" in nvim.api.win_get_option(current[0], "winhighlight")
    lines = nvim.api.buf_get_lines(nvim.api.win_get_buf(current[0]), 0, -1, True)
    assert any("Writing..." in line for line in lines)
    assert len(_floating_windows(nvim)) == 1, "a second status float was left behind"


@pytest.mark.integration
def test_the_status_float_follows_the_backend_into_a_new_tab(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the HOOK's order: _animate_edit shows the writer cue and
    "Writing..." BEFORE the backend navigates (show_fresh(in_new_tab=True)
    opens the new file's tab after the cue). Moving the float only when a cue
    is set therefore moved it to the tab being LEFT, and the animation ran in
    the new tab with no visible cue (whole-branch review, 2026-09-23)."""
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    surface = NvimStatusSurface(socket_path=headless_nvim)
    first = tmp_path / "first.py"
    first.write_text("a = 1\n")
    follower.show_fresh(str(first), "a = 1\n")

    second = tmp_path / "second.py"
    second.write_text("b = 2\n")
    surface.set_writer("code-reviewer", "colour78")  # the hook's cue order
    surface.set_state("Writing...")
    follower.show_fresh(str(second), "b = 2\n", in_new_tab=True)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert Path(nvim.api.buf_get_name(nvim.api.get_current_buf())).name == "second.py"
    here = [
        w
        for w in nvim.api.tabpage_list_wins(nvim.api.get_current_tabpage())
        if nvim.api.win_get_config(w)["relative"] != ""
    ]
    assert len(here) == 1, "the cue stayed behind in the tab the backend left"
    assert any("code-reviewer" in c[0] for c in nvim.api.win_get_config(here[0])["title"])
    assert len(_floating_windows(nvim)) == 1

    # A Read's navigation switches tabs through set_current_win, not :tabnew.
    follower.goto_file(str(first))
    assert Path(nvim.api.buf_get_name(nvim.api.get_current_buf())).name == "first.py"
    here = [
        w
        for w in nvim.api.tabpage_list_wins(nvim.api.get_current_tabpage())
        if nvim.api.win_get_config(w)["relative"] != ""
    ]
    assert len(here) == 1, "the cue stayed behind when goto_file switched tabs"
    assert len(_floating_windows(nvim)) == 1
