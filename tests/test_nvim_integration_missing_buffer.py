"""Real-headless-nvim coverage for the "buffer is not there" paths: Read
navigation to a never-seen file, an out-of-range Read offset, and an edit
whose buffer was wiped since the snapshot was taken. These only reproduce
against a real nvim — the crashes are raised by nvim itself, not by us."""

from __future__ import annotations

from pathlib import Path

import pytest

pynvim = pytest.importorskip("pynvim")

from test_nvim_integration import _tab_buffer_names  # noqa: E402

from vim_ai_follower.animate import AnimationResult  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.diff import compute_edit_script  # noqa: E402


@pytest.mark.integration
def test_ensure_showing_a_never_seen_file_shows_its_disk_content_in_a_new_tab(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    typed = tmp_path / "typed.py"
    typed.write_text("ON DISK, not what was typed\n")
    follower.show_fresh(str(typed), "TYPED ONLY\n")

    read_me = tmp_path / "read_me.py"
    read_me.write_text("disk1\ndisk2\ndisk3\n")
    follower.ensure_showing(str(read_me))

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["disk1", "disk2", "disk3"]
    names = _tab_buffer_names(nvim)
    assert any(name.endswith("/read_me.py") for name in names)
    # the typed-but-unsaved buffer in the other tab is untouched
    other = nvim.funcs.bufnr(str(typed))
    assert nvim.api.buf_get_lines(other, 0, -1, True) == ["TYPED ONLY"]


@pytest.mark.integration
def test_goto_line_past_eof_lands_on_the_last_line_instead_of_raising(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    short = tmp_path / "short.py"
    short.write_text("one\ntwo\n")
    follower.ensure_showing(str(short))
    follower.goto_line(500)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.window.cursor == (2, 0)


@pytest.mark.integration
def test_apply_edit_after_the_buffer_was_wiped_shows_the_disk_content(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    target = tmp_path / "wiped.py"
    before = "a\nb\nc\nd\ne\n"
    after = "a\nb\nC\nd\nE\n"
    # The PostToolUse hook runs after Claude's write, so disk holds `after`.
    target.write_text(after)
    follower.show_fresh(str(target), before)

    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.command(f"silent! bwipeout! {nvim.funcs.bufnr(str(target))}")
    assert nvim.funcs.bufnr(str(target)) == -1

    ops = compute_edit_script(before, after)
    result = follower.apply_edit(str(target), ops)

    assert result == AnimationResult("completed", len(ops))
    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["a", "b", "C", "d", "E"]
    assert nvim.current.buffer.name.endswith("/wiped.py")


@pytest.mark.integration
def test_ensure_showing_an_existing_modified_buffer_never_reloads_it(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    target = tmp_path / "live.py"
    target.write_text("STALE DISK CONTENT\n")
    follower.show_fresh(str(target), "typed line 1\ntyped line 2\n")
    follower.show_fresh(str(tmp_path / "other.py"), "other\n", in_new_tab=True)

    follower.ensure_showing(str(target))

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["typed line 1", "typed line 2"]
    assert nvim.current.buffer.name.endswith("/live.py")
