"""Real-headless-nvim coverage for the nvim backend's navigation lock: a
launched, dedicated follower's buffer gets relocked (`nomodifiable`) by
ensure_showing/apply_edit's disk-reading branches, exactly like the tmux
backend's `ensure_showing` (which relocks after `:tab drop`) — but an
ADOPTED nvim never is, navigation included (same rule `_drive`'s completion
relock follows; see nvim.py's docstrings). Reuses the headless_nvim fixture
and the tab-name helper from test_nvim_integration.py rather than
duplicating them."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from test_nvim_integration import _tab_buffer_names  # noqa: E402

from vim_ai_follower.animate import AnimationResult  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.diff import compute_edit_script  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402


def _modifiable(nvim: Any, buf: int) -> bool:
    value = nvim.api.buf_get_option(buf, "modifiable")
    return bool(value)


@pytest.mark.integration
def test_ensure_showing_a_never_seen_file_locks_the_loaded_buffer(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    read_me = tmp_path / "read_me.py"
    read_me.write_text("disk1\ndisk2\n")
    follower.ensure_showing(str(read_me))

    nvim = pynvim.attach("socket", path=headless_nvim)
    buf = nvim.funcs.bufnr(str(read_me))
    assert buf != -1
    assert _modifiable(nvim, buf) is False
    assert nvim.current.buffer[:] == ["disk1", "disk2"]  # content untouched by the lock


@pytest.mark.integration
def test_ensure_showing_a_never_seen_file_leaves_an_adopted_buffer_modifiable(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set("@1", "nvim", headless_nvim, adopted=True)
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    read_me = tmp_path / "read_me.py"
    read_me.write_text("disk1\ndisk2\n")
    follower.ensure_showing(str(read_me))

    nvim = pynvim.attach("socket", path=headless_nvim)
    buf = nvim.funcs.bufnr(str(read_me))
    assert buf != -1
    # an adopted nvim is the user's own editor: never locked out of it
    assert _modifiable(nvim, buf) is True
    assert nvim.current.buffer[:] == ["disk1", "disk2"]


@pytest.mark.integration
def test_ensure_showing_an_existing_unlocked_buffer_switches_to_it_and_locks_it(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    target = tmp_path / "live.py"
    target.write_text("STALE DISK CONTENT\n")
    follower.show_fresh(str(target), "typed line 1\ntyped line 2\n")
    nvim = pynvim.attach("socket", path=headless_nvim)
    buf = nvim.funcs.bufnr(str(target))
    # show_fresh's own completion already locked it; unlock it first (e.g.
    # hand_over after an interrupt) so this test actually exercises
    # ensure_showing's own lock rather than finding it already locked.
    follower.hand_over()
    assert _modifiable(nvim, buf) is True

    follower.show_fresh(str(tmp_path / "other.py"), "other\n", in_new_tab=True)
    follower.ensure_showing(str(target))

    assert nvim.current.buffer.name.endswith("/live.py")
    assert nvim.current.buffer[:] == ["typed line 1", "typed line 2"]  # content untouched
    assert _modifiable(nvim, buf) is False


@pytest.mark.integration
def test_ensure_showing_an_adopted_followers_existing_buffer_stays_modifiable(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set("@1", "nvim", headless_nvim, adopted=True)
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    target = tmp_path / "live.py"
    target.write_text("STALE DISK CONTENT\n")
    follower.show_fresh(str(target), "typed line 1\ntyped line 2\n")
    nvim = pynvim.attach("socket", path=headless_nvim)
    buf = nvim.funcs.bufnr(str(target))
    # show_fresh's own completion never locked it (adopted), so this test
    # actually exercises ensure_showing's own (non-)lock.
    assert _modifiable(nvim, buf) is True

    follower.show_fresh(str(tmp_path / "other.py"), "other\n", in_new_tab=True)
    follower.ensure_showing(str(target))

    assert nvim.current.buffer.name.endswith("/live.py")
    assert nvim.current.buffer[:] == ["typed line 1", "typed line 2"]  # content untouched
    assert _modifiable(nvim, buf) is True


@pytest.mark.integration
def test_apply_edit_on_a_vanished_buffer_locks_the_disk_loaded_buffer(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    target = tmp_path / "wiped.py"
    before = "a\nb\nc\n"
    after = "a\nB\nc\n"
    target.write_text(after)
    follower.show_fresh(str(target), before)

    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.command(f"silent! bwipeout! {nvim.funcs.bufnr(str(target))}")
    assert nvim.funcs.bufnr(str(target)) == -1

    ops = compute_edit_script(before, after)
    result = follower.apply_edit(str(target), ops)

    assert result == AnimationResult("completed", len(ops))
    buf = nvim.funcs.bufnr(str(target))
    assert buf != -1
    assert _modifiable(nvim, buf) is False
    assert nvim.current.buffer[:] == ["a", "B", "c"]


@pytest.mark.integration
def test_apply_edit_on_a_vanished_buffer_leaves_an_adopted_disk_loaded_buffer_modifiable(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set("@1", "nvim", headless_nvim, adopted=True)
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    target = tmp_path / "wiped.py"
    before = "a\nb\nc\n"
    after = "a\nB\nc\n"
    target.write_text(after)
    follower.show_fresh(str(target), before)

    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.command(f"silent! bwipeout! {nvim.funcs.bufnr(str(target))}")
    assert nvim.funcs.bufnr(str(target)) == -1

    ops = compute_edit_script(before, after)
    result = follower.apply_edit(str(target), ops)

    assert result == AnimationResult("completed", len(ops))
    buf = nvim.funcs.bufnr(str(target))
    assert buf != -1
    # an adopted nvim is the user's own editor: never locked out of it
    assert _modifiable(nvim, buf) is True
    assert nvim.current.buffer[:] == ["a", "B", "c"]


@pytest.mark.integration
def test_read_navigation_then_a_real_edit_on_the_same_file_still_animates(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock must not be a dead end: apply_edit's own _drive envelope has
    to be able to unlock a buffer that ensure_showing just locked and
    complete a real animation on it, exactly like following a Read with a
    live edit on the tmux backend."""
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    target = tmp_path / "read_then_edit.py"
    before = "a\nb\nc\n"
    target.write_text(before)

    # Read navigation: never-seen file, loaded from disk and locked.
    follower.ensure_showing(str(target))
    nvim = pynvim.attach("socket", path=headless_nvim)
    buf = nvim.funcs.bufnr(str(target))
    assert _modifiable(nvim, buf) is False

    # A cursor move (goto_line, as the real Read hook does) must not choke
    # on the locked buffer.
    follower.goto_line(2)
    assert nvim.current.window.cursor == (2, 0)

    # A real edit on that same file must still unlock, animate, and relock.
    after = "a\nB\nc\n"
    target.write_text(after)  # PostToolUse runs after the write
    ops = compute_edit_script(before, after)
    result = follower.apply_edit(str(target), ops)

    assert result == AnimationResult("completed", len(ops))
    assert nvim.current.buffer[:] == ["a", "B", "c"]
    assert _modifiable(nvim, buf) is False  # relocked on completion


@pytest.mark.integration
def test_read_navigation_then_show_fresh_on_the_same_file_still_animates(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    target = tmp_path / "read_then_fresh.py"
    target.write_text("original disk content\n")

    follower.ensure_showing(str(target))
    nvim = pynvim.attach("socket", path=headless_nvim)
    assert _modifiable(nvim, nvim.funcs.bufnr(str(target))) is False

    result = follower.show_fresh(str(target), "brand new content\n")

    assert result == AnimationResult("completed", 1)
    names = _tab_buffer_names(nvim)
    assert any(name.endswith("/read_then_fresh.py") for name in names)
    assert nvim.current.buffer[:] == ["brand new content"]
    assert _modifiable(nvim, nvim.funcs.bufnr(str(target))) is False
