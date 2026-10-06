"""Real-nvim twin of tests/test_integration_show_fresh_tab_jump.py: in an
ADOPTED nvim whose user jumps tabs on a new empty buffer, show_fresh names,
reads and types into the buffer it CREATED (by handle), never "the current
buffer after `:tabnew`" (final review of backlog-sweep-3, I1: at acce2b2 the
user's buffer 1 was renamed jump.py and its unsaved line replaced)."""

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


def test_a_tabnew_that_jumps_back_never_renames_the_users_buffer(
    headless_nvim: str, tmp_path: Path
) -> None:
    root = Path(os.path.realpath(tmp_path))
    user = root / "user.txt"
    user.write_text("user line\n")
    target = root / "jump.py"
    target.write_text("jump = 1\n")
    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.command(f"edit {user}")
    nvim.command(
        "autocmd BufEnter * if bufname('%') ==# '' && tabpagenr('$') > 1 | tabfirst | endif"
    )
    nvim.current.buffer.append("USER UNSAVED")
    user_buffer = nvim.current.buffer
    FollowerState.set("@77", backend="nvim", target=headless_nvim, adopted=True, speed="instant")
    follower = NvimFollower(socket_path=headless_nvim, window_id="@77", pace_seconds=0.0)

    assert follower.show_fresh(str(target), "jump = 1\n", in_new_tab=True).outcome == "completed"

    assert os.path.realpath(user_buffer.name) == str(user)
    assert user_buffer[:] == ["user line", "USER UNSAVED"]
    typed = [
        buffer
        for buffer in nvim.buffers
        if buffer.name and os.path.realpath(buffer.name) == str(target)
    ]
    assert [buffer[:] for buffer in typed] == [["jump = 1"]]
    # No empty buffer left behind by the tabnew.
    assert [buffer.number for buffer in nvim.buffers if buffer.name == ""] == []


# The autocmds of the final review of backlog-sweep-3 (N2/N3). BufEnter:
# every empty buffer entered sends the cursor back to the first tab, AFTER
# the tabnew made its tab. TabNew/BufNew: the jump runs inside the tabnew,
# before its empty buffer is entered, so nvim itself puts that buffer in the
# user's window and leaves the user's buffer in the new tab.
_JUMPS = {
    "bufenter": "autocmd BufEnter * if bufname('%') ==# '' | tabfirst | endif",
    "tabnew": "autocmd TabNew * tabfirst",
    "bufnew": "autocmd BufNew * tabfirst",
}


def _adopted_with_jump(
    headless_nvim: str, root: Path, jump: str = "bufenter", hidden: bool = True
) -> tuple[Any, Any]:
    """An adopted nvim whose user edits user.txt (one unsaved line) in its
    one window, with a tab-jumping autocmd; the user's buffer."""
    user = root / "user.txt"
    user.write_text("user line\n")
    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.command(f"edit {user}")
    nvim.current.buffer.append("USER UNSAVED")
    if not hidden:
        nvim.command("set nohidden")
    nvim.command(_JUMPS[jump])
    FollowerState.set("@77", backend="nvim", target=headless_nvim, adopted=True, speed="instant")
    return nvim, nvim.current.buffer


def _follower(headless_nvim: str) -> NvimFollower:
    return NvimFollower(socket_path=headless_nvim, window_id="@77", pace_seconds=0.0)


def _assert_new_tab_shows(nvim: Any, user_buffer: Any, user_window: Any, target: Path) -> None:
    """target is current, in a second tab; the user's window still shows
    their intact buffer and carries no follower mark."""
    assert os.path.realpath(nvim.current.buffer.name) == str(target)
    assert len(nvim.api.list_tabpages()) == 2
    assert nvim.current.tabpage.number == 2
    assert nvim.api.win_get_buf(user_window) == user_buffer
    assert user_buffer[:] == ["user line", "USER UNSAVED"]
    assert nvim.funcs.getwinvar(user_window.handle, "vaf_opened", 0) == 0


def _unshown_empty_buffers(nvim: Any) -> list[int]:
    return [
        buffer.number
        for buffer in nvim.buffers
        if buffer.name == "" and not nvim.funcs.win_findbuf(buffer.number)
    ]


def test_goto_file_shows_a_hidden_buffer_in_a_new_tab_not_the_users_window(
    headless_nvim: str, tmp_path: Path
) -> None:
    root = Path(os.path.realpath(tmp_path))
    target = root / "hidden.py"
    target.write_text("hidden = 1\n")
    pynvim.attach("socket", path=headless_nvim).command(f"badd {target}")
    nvim, user_buffer = _adopted_with_jump(headless_nvim, root)
    user_window = nvim.current.window

    _follower(headless_nvim).goto_file(str(target))

    _assert_new_tab_shows(nvim, user_buffer, user_window, target)
    assert nvim.funcs.getwinvar(nvim.current.window.handle, "vaf_opened", 0) == 1
    assert _unshown_empty_buffers(nvim) == []


@pytest.mark.parametrize("on_disk", [True, False], ids=["exists", "new-file"])
def test_goto_file_creates_a_missing_buffer_in_a_new_tab_not_the_users_window(
    headless_nvim: str, tmp_path: Path, on_disk: bool
) -> None:
    root = Path(os.path.realpath(tmp_path))
    target = root / "made.py"
    if on_disk:
        target.write_text("made = 1\n")
    nvim, user_buffer = _adopted_with_jump(headless_nvim, root)
    user_window = nvim.current.window

    _follower(headless_nvim).goto_file(str(target))

    _assert_new_tab_shows(nvim, user_buffer, user_window, target)
    # goto_file still never SHOWS the file's content: the buffer is empty.
    assert nvim.current.buffer[:] == [""]
    assert _unshown_empty_buffers(nvim) == []


@pytest.mark.parametrize("hidden", [True, False], ids=["hidden", "nohidden"])
@pytest.mark.parametrize("jump", list(_JUMPS))
def test_a_read_of_an_unopened_file_opens_one_tab_and_leaves_the_users_window(
    headless_nvim: str, tmp_path: Path, jump: str, hidden: bool
) -> None:
    """At 0270eb4: under the BufEnter jump Claude's file took over the
    user's window (and under 'nohidden' the Read raised a raw NvimError
    E37); under TabNew/BufNew the user's buffer was pushed out of its window
    into the new tab; and every Read left an empty buffer behind."""
    root = Path(os.path.realpath(tmp_path))
    target = root / "read.py"
    target.write_text("read = 1\n")
    nvim, user_buffer = _adopted_with_jump(headless_nvim, root, jump, hidden)
    user_window = nvim.current.window

    _follower(headless_nvim).ensure_showing(str(target))

    _assert_new_tab_shows(nvim, user_buffer, user_window, target)
    assert nvim.current.buffer[:] == ["read = 1"]
    assert _unshown_empty_buffers(nvim) == []
    assert [buffer.number for buffer in nvim.buffers if buffer.name == ""] == []


@pytest.mark.parametrize("jump", ["tabnew", "bufnew"])
def test_show_fresh_under_an_early_jump_gives_the_user_their_window_back(
    headless_nvim: str, tmp_path: Path, jump: str
) -> None:
    """N2: under 'nohidden' this raised a raw NvimError E37 and left a
    named, empty buffer; under 'hidden' it hid the user's buffer."""
    root = Path(os.path.realpath(tmp_path))
    target = root / "jump.py"
    target.write_text("jump = 1\n")
    nvim, user_buffer = _adopted_with_jump(headless_nvim, root, jump, hidden=False)
    user_window = nvim.current.window

    result = _follower(headless_nvim).show_fresh(str(target), "jump = 1\n", in_new_tab=True)

    assert result.outcome == "completed"
    _assert_new_tab_shows(nvim, user_buffer, user_window, target)
    assert [buffer.number for buffer in nvim.buffers if buffer.name == ""] == []


def test_under_nohidden_a_refused_switch_is_navigation_failed_not_a_raw_error(
    headless_nvim: str, tmp_path: Path
) -> None:
    """An adopted follower's first Write types in the CURRENT window
    (in_new_tab=False). Under 'nohidden', with the user's buffer there
    modified, nvim refuses the switch (E37): that used to escape as a raw
    NvimError and leave the follower's named, empty buffer behind."""
    root = Path(os.path.realpath(tmp_path))
    target = root / "first.py"
    target.write_text("first = 1\n")
    nvim, user_buffer = _adopted_with_jump(headless_nvim, root, hidden=False)
    user_window = nvim.current.window

    with pytest.raises(NavigationFailed):
        _follower(headless_nvim).show_fresh(str(target), "first = 1\n")

    assert nvim.api.win_get_buf(user_window) == user_buffer
    assert user_buffer[:] == ["user line", "USER UNSAVED"]
    assert [buffer.number for buffer in nvim.buffers if buffer.number != user_buffer.number] == []
