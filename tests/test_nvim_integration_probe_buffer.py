"""NvimFollower.probe_buffer against a real headless nvim: the answer the
base-mismatch guard in hooks._animate_edit acts on. nvim can read its own
buffer over RPC, so this is a direct comparison, no probe file."""

from __future__ import annotations

from pathlib import Path

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402

pytestmark = pytest.mark.integration


def _follower(headless_nvim: str) -> NvimFollower:
    return NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)


def test_a_buffer_holding_the_content_holds_it(headless_nvim: str, tmp_path: Path) -> None:
    target = tmp_path / "f.py"
    follower = _follower(headless_nvim)
    follower.show_fresh(str(target), "a\nb\n")
    assert follower.probe_buffer(str(target), "a\nb\n") == "holds"


def test_a_buffer_holding_other_content_differs(headless_nvim: str, tmp_path: Path) -> None:
    """The outside-change case: the buffer still holds what the follower
    typed, while the new base is what a formatter wrote to disk."""
    target = tmp_path / "f.py"
    follower = _follower(headless_nvim)
    follower.show_fresh(str(target), "a\nb\n")
    assert follower.probe_buffer(str(target), "a\nFORMATTED\n") == "differs"


def test_a_file_no_buffer_holds_is_absent(headless_nvim: str, tmp_path: Path) -> None:
    assert _follower(headless_nvim).probe_buffer(str(tmp_path / "nope.py"), "") == "absent"


def test_an_unloaded_buffer_is_absent(headless_nvim: str, tmp_path: Path) -> None:
    """Under 'nohidden' a closed tab unloads its buffer; goto_file's
    win_set_buf would then load the finished file from disk."""
    target = tmp_path / "f.py"
    target.write_text("a\nb\n")
    follower = _follower(headless_nvim)
    follower.ensure_showing(str(target))
    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.command("tabnew")
    buf = nvim.funcs.bufadd(str(target))
    nvim.command(f"bunload! {buf}")
    assert nvim.api.buf_is_loaded(buf) is False
    assert follower.probe_buffer(str(target), "a\nb\n") == "absent"
