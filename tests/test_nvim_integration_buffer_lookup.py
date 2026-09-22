"""Real-headless-nvim coverage for finding "the buffer for this file".

`bufnr()` and `:bwipeout {name}` take a buffer-name PATTERN, not a path.
Measured 2026-09-22 against real nvim: with `app/[slug]/page.tsx` and its
pattern-sibling `app/s/page.tsx` both open, `bufnr(target)` returns the
SIBLING, and `:bwipeout! target` wipes the sibling while the target survives.
A path with a space splits into two Ex arguments; `*`/`?` glob. Only an exact
buffer-list walk is immune — and it must still see through a symlinked
directory prefix (macOS /tmp -> /private/tmp), because nvim stores buffer
names with the prefix resolved."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402

# (target, a sibling its name matches as a Vim file-pattern)
_PATTERN_PAIRS = [
    ("app/[slug]/page.tsx", "app/s/page.tsx"),
    ("x/{a,b}.py", "x/a.py"),
    ("f*.py", "foo.py"),
]

_EX_HOSTILE_NAMES = ["a b.py", "a#1.py", "p%.py", "app/[slug]/page.tsx"]


def _buffers_for(nvim: Any, path: Path) -> list[int]:
    want = os.path.realpath(path)
    return [
        buf.number
        for buf in nvim.api.list_bufs()
        if nvim.api.buf_get_name(buf) and os.path.realpath(nvim.api.buf_get_name(buf)) == want
    ]


def _follower(headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NvimFollower:
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    return NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)


def _make(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.integration
@pytest.mark.parametrize(("target_rel", "sibling_rel"), _PATTERN_PAIRS)
def test_close_tab_evicts_the_file_not_its_pattern_sibling(
    headless_nvim: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_rel: str,
    sibling_rel: str,
) -> None:
    follower = _follower(headless_nvim, tmp_path, monkeypatch)
    sibling = _make(tmp_path, sibling_rel, "SIB\n")
    target = _make(tmp_path, target_rel, "TARGET\n")
    follower.show_fresh(str(sibling), "SIB\n", in_new_tab=True)
    follower.show_fresh(str(target), "TARGET\n", in_new_tab=True)

    follower.close_tab(str(target))

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert _buffers_for(nvim, target) == []
    [sib] = _buffers_for(nvim, sibling)
    assert nvim.api.buf_get_lines(sib, 0, -1, True) == ["SIB"]


@pytest.mark.integration
@pytest.mark.parametrize(("target_rel", "sibling_rel"), _PATTERN_PAIRS)
def test_goto_file_lands_on_the_file_not_its_pattern_sibling(
    headless_nvim: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_rel: str,
    sibling_rel: str,
) -> None:
    follower = _follower(headless_nvim, tmp_path, monkeypatch)
    target = _make(tmp_path, target_rel, "TARGET\n")
    sibling = _make(tmp_path, sibling_rel, "SIB\n")
    follower.show_fresh(str(target), "TARGET\n", in_new_tab=True)
    follower.show_fresh(str(sibling), "SIB\n", in_new_tab=True)

    follower.goto_file(str(target))

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["TARGET"]
    assert len(_buffers_for(nvim, target)) == 1


@pytest.mark.integration
@pytest.mark.parametrize("name", _EX_HOSTILE_NAMES)
def test_show_fresh_over_an_existing_buffer_leaves_exactly_one_for_the_path(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    follower = _follower(headless_nvim, tmp_path, monkeypatch)
    path = _make(tmp_path, name, "NEW\n")
    follower.show_fresh(str(path), "OLD\n", in_new_tab=True)

    follower.show_fresh(str(path), "NEW\n", in_new_tab=True)

    nvim = pynvim.attach("socket", path=headless_nvim)
    [buf] = _buffers_for(nvim, path)
    assert nvim.api.buf_get_lines(buf, 0, -1, True) == ["NEW"]
    assert nvim.current.buffer.number == buf


@pytest.mark.integration
def test_lookup_sees_through_a_symlinked_directory_prefix(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the old code used bufnr(): nvim stores the name with the
    directory symlink resolved, so a naive name compare misses the buffer and
    goto_file opens a duplicate tab / close_tab evicts nothing."""
    follower = _follower(headless_nvim, tmp_path, monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    via_link = tmp_path / "link" / "f.py"
    via_link.write_text("F\n")
    other = _make(tmp_path, "other.py", "O\n")
    follower.show_fresh(str(via_link), "F\n", in_new_tab=True)
    follower.show_fresh(str(other), "O\n", in_new_tab=True)

    follower.goto_file(str(via_link))
    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["F"]
    assert len(nvim.api.list_tabpages()) == 3  # initial + two show_fresh, no duplicate

    follower.close_tab(str(via_link))
    assert _buffers_for(nvim, via_link) == []


@pytest.mark.integration
def test_lookup_finds_a_buffer_whose_file_is_not_on_disk_behind_a_symlinked_dir(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fs_realpath returns nil for a missing file, so the lookup resolves the
    directory and re-attaches the basename; without that, the resolved buffer
    name never equals the unresolved query and goto_file opens a duplicate."""
    follower = _follower(headless_nvim, tmp_path, monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    ghost = tmp_path / "link" / "ghost.py"  # never written to disk
    other = _make(tmp_path, "other.py", "O\n")

    follower.goto_file(str(ghost))
    follower.show_fresh(str(other), "O\n", in_new_tab=True)
    follower.goto_file(str(ghost))

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert not ghost.exists()
    assert len(nvim.api.list_tabpages()) == 3  # initial + ghost + other, no duplicate
    assert Path(nvim.api.buf_get_name(nvim.current.buffer)).name == "ghost.py"
