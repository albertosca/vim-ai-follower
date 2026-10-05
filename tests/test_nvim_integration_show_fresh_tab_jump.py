"""Real-nvim twin of tests/test_integration_show_fresh_tab_jump.py: in an
ADOPTED nvim whose user jumps tabs on a new empty buffer, show_fresh names,
reads and types into the buffer it CREATED (by handle), never "the current
buffer after `:tabnew`" (final review of backlog-sweep-3, I1: at acce2b2 the
user's buffer 1 was renamed jump.py and its unsaved line replaced)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pynvim = pytest.importorskip("pynvim")

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
