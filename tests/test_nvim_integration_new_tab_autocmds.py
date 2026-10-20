"""A new tab the follower opens under a user autocommand that makes it fail
leaves nothing behind (final review of backlog-sweep-4, T8 matrix m1-m4).

m1/m3: a `WinEnter * tabfirst`, or a TabNew that throws, made the new-tab
path raise NavigationFailed with the tab it had added still open. m2: a
BufAdd that wipes the buffer just created surfaced the raw `create_buf`
error. m4: a TabNew that switches a window of the user's to an empty buffer
on purpose had it switched back."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower.backends import NavigationFailed  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402

pytestmark = pytest.mark.integration

_FAILING = {
    "winenter-tabfirst": "autocmd WinEnter * tabfirst",
    "tabnew-throws": "autocmd TabNew * throw 'user TabNew failed'",
}


def _adopted(headless_nvim: str, root: Path) -> tuple[Any, NvimFollower]:
    user = root / "user.txt"
    user.write_text("user line\n")
    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.command(f"edit {user}")
    FollowerState.set("@78", backend="nvim", target=headless_nvim, adopted=True, speed="instant")
    return nvim, NvimFollower(socket_path=headless_nvim, window_id="@78", pace_seconds=0.0)


def _tab_layout(nvim: Any) -> list[list[int]]:
    return [[window.buffer.handle for window in tab.windows] for tab in nvim.tabpages]


@pytest.mark.parametrize("autocmd", list(_FAILING))
@pytest.mark.parametrize("entry", ["show_fresh", "read", "read_hidden_buffer"])
def test_a_failed_new_tab_leaves_no_tab(
    headless_nvim: str, tmp_path: Path, autocmd: str, entry: str
) -> None:
    root = Path(os.path.realpath(tmp_path))
    target = root / "target.py"
    target.write_text("target = 1\n")
    nvim, follower = _adopted(headless_nvim, root)
    if entry == "read_hidden_buffer":
        # A buffer the user already holds, shown in no window: the caller
        # never wipes it, so only the new-tab path can take its tab back.
        nvim.command(f"badd {target}")
    nvim.command(_FAILING[autocmd])
    tabs_before = _tab_layout(nvim)

    with pytest.raises(NavigationFailed):
        if entry == "show_fresh":
            follower.show_fresh(str(target), "target = 1\n", in_new_tab=True)
        else:
            follower.ensure_showing(str(target))

    nvim.command("autocmd! WinEnter")
    nvim.command("autocmd! TabNew")
    assert _tab_layout(nvim) == tabs_before


@pytest.mark.parametrize("entry", ["show_fresh", "read", "goto_missing"])
def test_a_bufadd_that_wipes_the_new_buffer_is_navigation_failed(
    headless_nvim: str, tmp_path: Path, entry: str
) -> None:
    root = Path(os.path.realpath(tmp_path))
    target = root / "target.py"
    if entry != "goto_missing":
        target.write_text("target = 1\n")
    nvim, follower = _adopted(headless_nvim, root)
    nvim.command("autocmd BufAdd * execute 'silent! bwipeout!' expand('<abuf>')")
    tabs_before = _tab_layout(nvim)

    with pytest.raises(NavigationFailed):
        if entry == "show_fresh":
            follower.show_fresh(str(target), "target = 1\n", in_new_tab=True)
        elif entry == "read":
            follower.ensure_showing(str(target))
        else:
            follower.goto_file(str(target))

    nvim.command("autocmd! BufAdd")
    assert _tab_layout(nvim) == tabs_before
