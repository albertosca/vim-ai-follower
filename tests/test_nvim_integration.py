from __future__ import annotations

from pathlib import Path

import pytest

pynvim = pytest.importorskip("pynvim")

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
