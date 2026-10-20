"""The nvim twin of tests/test_integration_handoff_write.py: after an
interrupted show_fresh (a Write-created file), plain `:w` must save the
handed-over buffer, and must still warn when the file changed outside since.

show_fresh names its buffer with `nvim_buf_set_name`, which, like Vim's
`:file`, marks the buffer "not edited": a plain `:w` over the existing file
raised `E13: File exists (add ! to override)`. The hand-off release itself is
backend-agnostic (hooks polls the file's mtime), so the save landing on disk
is what this file has to prove.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import cache, control  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402

pytestmark = pytest.mark.integration

CONTENT = "".join(f"line_{number} = {number}\n" for number in range(1, 7))


def _interrupt_at(nth: int) -> Any:
    calls = {"n": 0}

    def _signal(window_id: str, base_directory: Path | None = None) -> str | None:
        calls["n"] += 1
        return "interrupt" if calls["n"] == nth else None

    return _signal


def _interrupted_write(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Path]:
    monkeypatch.setattr(cache, "CACHE_DIRECTORY", tmp_path / "cache")
    target = tmp_path / "new.py"
    target.write_text(CONTENT)  # Claude's Write lands before the post hook
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    monkeypatch.setattr(control, "check_signal", _interrupt_at(3))
    result = follower.show_fresh(str(target), CONTENT)
    assert result.outcome == "interrupted"
    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] != CONTENT.splitlines()
    return nvim, target


def test_plain_w_saves_the_handed_over_buffer(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nvim, target = _interrupted_write(headless_nvim, tmp_path, monkeypatch)
    handed_over = nvim.current.buffer[:]

    nvim.command("w")

    assert target.read_text().splitlines() == handed_over


def test_plain_w_still_warns_when_the_file_changed_outside_since(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nvim, target = _interrupted_write(headless_nvim, tmp_path, monkeypatch)
    time.sleep(1.1)  # mtimes may compare in whole seconds
    target.write_text("changed outside\n")

    # The warning is a y/n question: send `:w` without waiting for it, and
    # answer from a second channel once nvim is sitting at the prompt.
    nvim.command("w", async_=True)
    other = pynvim.attach("socket", path=headless_nvim)
    deadline = time.monotonic() + 5.0
    while other.api.get_mode()["mode"] != "r?" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert other.api.get_mode()["mode"] == "r?", "no confirmation was asked"
    other.input("n")
    time.sleep(0.3)
    assert "changed since reading" in other.command_output("messages")
    assert target.read_text() == "changed outside\n"


@pytest.mark.parametrize("on_disk", [True, False], ids=["exists", "new-file"])
def test_plain_w_saves_a_buffer_goto_file_created(
    headless_nvim: str, tmp_path: Path, on_disk: bool
) -> None:
    """C2-C5 (c): goto_file's "no buffer at all" branch named a new buffer,
    which left it "not edited", so a plain `:w` over the existing file hit
    E13 exactly as show_fresh's did before its read-then-clear."""
    target = tmp_path / "made.py"
    if on_disk:
        target.write_text("before = 1\n")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    follower.goto_file(str(target))

    other = pynvim.attach("socket", path=headless_nvim)
    other.current.buffer[:] = ["typed = 1"]
    other.command("w")

    assert target.read_text() == "typed = 1\n"
