"""A Read re-reads a CLEAN open buffer on nvim too (parity with the tmux
backend's `_RELOAD_IF_CLEAN`), against real nvims: a file rewritten outside
Claude's Edits (a formatter, `sed -i`, a `git checkout`) is shown as it is on
disk, and a buffer holding the user's unsaved typing is never touched.

"Clean" cannot be nvim's 'modified' alone: the follower never writes its
buffers, so every buffer it typed is 'modified' although it matches the file
Claude wrote. A completed animation stamps `b:changedtick`; a buffer whose
tick still equals the stamp holds only follower text.

The adopted cases run on the swap-aware fixture (the shared headless_nvim has
`-n`, which would make any swap assertion vacuous), with a second live nvim
holding the file's swap: the re-read must keep the user's buffer on its own
swap and raise nothing. The UI-nvim version of that (no ATTENTION on screen)
lives in tests/test_e2e_adopted_swap.py.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from test_nvim_integration_swap import _owner_holding, swap_aware_nvim  # noqa: E402

from vim_ai_follower import cache, state  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402

pytestmark = pytest.mark.integration

__all__ = ["swap_aware_nvim"]  # the imported fixture is used by name below

WRITTEN = "def f( x ):\n    return x\n"
FORMATTED = "def f(x):\n    return x\n"


def _buffer(nvim: Any, target: Path) -> list[str]:
    bufnr = nvim.funcs.bufnr(str(target))
    lines: list[str] = nvim.api.buf_get_lines(bufnr, 0, -1, True)
    return lines


@pytest.fixture
def dedicated(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[NvimFollower, Any]:
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    state.FollowerState.set("@1", "nvim", headless_nvim)
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    return follower, pynvim.attach("socket", path=headless_nvim)


def test_a_read_reloads_a_buffer_the_follower_typed_once_disk_moved_on(
    dedicated: tuple[NvimFollower, Any], tmp_path: Path
) -> None:
    follower, nvim = dedicated
    target = tmp_path / "f.py"
    target.write_text(WRITTEN)
    assert follower.show_fresh(str(target), WRITTEN).outcome == "completed"
    # The premise: a follower-typed buffer is 'modified' from nvim's view.
    assert nvim.api.buf_get_option(nvim.funcs.bufnr(str(target)), "modified") is True

    target.write_text(FORMATTED)  # a formatter run through Bash
    follower.ensure_showing(str(target))

    assert _buffer(nvim, target) == FORMATTED.splitlines()


def test_a_read_reloads_a_clean_buffer_it_opened_from_disk(
    dedicated: tuple[NvimFollower, Any], tmp_path: Path
) -> None:
    follower, nvim = dedicated
    target = tmp_path / "f.py"
    target.write_text(WRITTEN)
    follower.ensure_showing(str(target))
    assert _buffer(nvim, target) == WRITTEN.splitlines()

    target.write_text(FORMATTED)
    follower.ensure_showing(str(target))

    assert _buffer(nvim, target) == FORMATTED.splitlines()


def test_a_read_never_reloads_a_buffer_the_user_typed_into(
    dedicated: tuple[NvimFollower, Any], tmp_path: Path
) -> None:
    follower, nvim = dedicated
    target = tmp_path / "f.py"
    target.write_text(WRITTEN)
    assert follower.show_fresh(str(target), WRITTEN).outcome == "completed"
    # The user unlocks the follower's buffer and types, unsaved.
    bufnr = nvim.funcs.bufnr(str(target))
    nvim.api.buf_set_option(bufnr, "modifiable", True)
    nvim.api.buf_set_lines(bufnr, 0, 0, True, ["# mine"])
    typed = ["# mine", *WRITTEN.splitlines()]

    target.write_text(FORMATTED)
    follower.ensure_showing(str(target))

    assert _buffer(nvim, target) == typed


def _adopted_on(
    swap_aware_nvim: Callable[[], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[NvimFollower, Any, Path]:
    """An adopted nvim with the target open as the user would have it: loaded
    with its own swap, while a second live nvim holds the file's main swap."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    target = tmp_path / "held.py"
    target.write_text(WRITTEN)
    _owner_holding(swap_aware_nvim(), target)
    sock = swap_aware_nvim()
    nvim = pynvim.attach("socket", path=sock)
    # The user opened it and answered the ATTENTION dialog "edit anyway".
    nvim.command("set shortmess+=A")
    nvim.command(f"edit {target}")
    nvim.command("set shortmess-=A")
    assert nvim.funcs.swapname(nvim.funcs.bufnr(str(target))) != ""
    state.FollowerState.set("@1", "nvim", sock, adopted=True)
    follower = NvimFollower(socket_path=sock, window_id="@1", pace_seconds=0.0)
    return follower, nvim, target


def test_an_adopted_nvim_reloads_a_clean_buffer_without_a_swap_prompt(
    swap_aware_nvim: Callable[[], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    follower, nvim, target = _adopted_on(swap_aware_nvim, tmp_path, monkeypatch)

    target.write_text(FORMATTED)
    follower.ensure_showing(str(target))

    assert _buffer(nvim, target) == FORMATTED.splitlines()
    assert nvim.api.get_mode()["blocking"] is False
    bufnr = nvim.funcs.bufnr(str(target))
    # The user's own buffer keeps its swap, and is never locked.
    assert nvim.funcs.swapname(bufnr) != ""
    assert nvim.api.buf_get_option(bufnr, "modifiable") is True


def test_an_adopted_nvim_never_reloads_the_users_unsaved_typing(
    swap_aware_nvim: Callable[[], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    follower, nvim, target = _adopted_on(swap_aware_nvim, tmp_path, monkeypatch)
    bufnr = nvim.funcs.bufnr(str(target))
    nvim.api.buf_set_lines(bufnr, 0, 0, True, ["# mine"])
    typed = ["# mine", *WRITTEN.splitlines()]

    target.write_text(FORMATTED)
    follower.ensure_showing(str(target))

    assert _buffer(nvim, target) == typed
    assert nvim.api.get_mode()["blocking"] is False


def test_an_adopted_nvim_reports_the_users_readonly_for_the_hand_off_cue(
    swap_aware_nvim: Callable[[], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    follower, nvim, target = _adopted_on(swap_aware_nvim, tmp_path, monkeypatch)
    assert follower.user_readonly(str(target)) is False
    nvim.command(f"view {target}")
    assert follower.user_readonly(str(target)) is True
