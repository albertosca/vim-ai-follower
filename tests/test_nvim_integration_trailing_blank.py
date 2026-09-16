"""Real-nvim regression coverage for the trailing-blank-line bug: an
apply_edit op whose deletion empties the buffer down to nvim's implicit
single blank line must not leave that blank line stranded at the bottom
after the new content is typed in. Reuses the headless_nvim fixture and
helpers from test_nvim_integration.py rather than duplicating them."""

from __future__ import annotations

from pathlib import Path

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import control  # noqa: E402
from vim_ai_follower.animate import AnimationResult  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.diff import compute_edit_script  # noqa: E402


@pytest.mark.integration
def test_apply_edit_on_a_single_line_file_leaves_no_trailing_blank(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exact scenario from the bug report: a one-line buffer, replaced
    # entirely, used to end at ["a = 2", ""] instead of ["a = 2"] — nvim
    # can't hold zero lines, so deleting the sole line left an implicit
    # blank that _animate_lines' insert-before-row mechanism then pushed
    # past the retyped content instead of it being consumed.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    follower.show_fresh("/tmp/x.py", "a = 1\n")
    ops = compute_edit_script("a = 1\n", "a = 2\n")
    result = follower.apply_edit("/tmp/x.py", ops)
    assert result == AnimationResult("completed", 1)

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["a = 2"]


@pytest.mark.integration
def test_apply_edit_that_wipes_a_multiline_buffer_leaves_no_trailing_blank(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same mechanism, not a single-line file: a 3-line buffer entirely
    # replaced by 2 unrelated lines, proving the fix isn't special-cased to
    # a buffer of exactly one line.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    before = "a\nb\nc\n"
    after = "x\ny\n"
    follower.show_fresh("/tmp/x.py", before)
    ops = compute_edit_script(before, after)
    result = follower.apply_edit("/tmp/x.py", ops)
    assert result == AnimationResult("completed", len(ops))

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["x", "y"]


@pytest.mark.integration
def test_apply_edit_deletion_that_leaves_other_lines_is_unaffected(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Control case: the op's own deletion does not empty the WHOLE buffer
    # (line 1 and line 3 survive), so nvim never falls back to an implicit
    # blank line and the fix must not touch this path at all.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    before = "a\nb\nc\n"
    after = "a\nB\nc\n"
    follower.show_fresh("/tmp/x.py", before)
    ops = compute_edit_script(before, after)
    result = follower.apply_edit("/tmp/x.py", ops)
    assert result == AnimationResult("completed", len(ops))

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["a", "B", "c"]


@pytest.mark.integration
def test_apply_edit_preserves_a_genuinely_intended_trailing_blank_line(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real trailing blank line in the new content (after ends with "\n\n",
    # so splitlines() yields a final "") must survive the fix: it sits
    # BEFORE the position the implicit blank gets pushed to, not at it.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)

    before = "a = 1\n"
    after = "a = 2\n\n"
    follower.show_fresh("/tmp/x.py", before)
    ops = compute_edit_script(before, after)
    result = follower.apply_edit("/tmp/x.py", ops)
    assert result == AnimationResult("completed", len(ops))

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["a = 2", ""]


@pytest.mark.integration
def test_apply_edit_interrupted_mid_wipe_leaves_a_sane_buffer(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An interrupt partway through an op that wipes the whole buffer must
    # not crash and must not leave the buffer in a broken (missing/negative
    # range) state — the higher-level hooks.py flow (rewrite_buffer + a
    # fresh resume) is what lands the final content, not this path, but it
    # first needs a buffer nvim itself is happy to keep operating on.
    from vim_ai_follower import cache

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.05)

    follower.show_fresh("/tmp/x.py", "a = 1\n")
    ops = compute_edit_script("a = 1\n", "a = 2222\n")

    calls = {"n": 0}

    def _interrupt_midway(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "interrupt" if calls["n"] == 3 else None

    monkeypatch.setattr(control, "check_signal", _interrupt_midway)
    result = follower.apply_edit("/tmp/x.py", ops)
    assert result.outcome == "interrupted"

    nvim = pynvim.attach("socket", path=headless_nvim)
    # nvim is still alive and answering RPC calls; the buffer holds at
    # least one line (never zero — measured, not assumed) with no crash.
    assert nvim.api.buf_line_count(nvim.current.buffer.handle) >= 1
