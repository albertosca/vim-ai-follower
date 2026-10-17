"""The first Edit into a freshly started DEDICATED nvim whose user config
shows a start screen (Task 24 item 1 of backlog-sweep-4).

A start screen (Alberto's `s:StartScreen`, dashboard/alpha/mini.starter
alike) runs `:enew` on the startup buffer, which nvim REUSES because buffer
1 is empty and unnamed, and sets `bufhidden=wipe buftype=nofile` on it. The
first show_fresh puts its own buffer in that window, nvim wipes buffer 1 on
the spot, and the scratch check that followed asked for the name of a buffer
that no longer existed: "hook post crashed ... Invalid buffer id: 1", the
first edit's animation lost."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

pynvim = pytest.importorskip("pynvim")

from test_nvim_integration_edit_base_mismatch import _ENV, _WINDOW, _payload  # noqa: E402

from vim_ai_follower import hooks  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402

pytestmark = pytest.mark.integration

_START_SCREEN = """
enew
setlocal bufhidden=wipe buftype=nofile nobuflisted noswapfile
call setline(1, ['', '    Press any key to start...'])
setlocal nomodifiable
"""


def _run_hook(hook_command: str, target: Path) -> None:
    payload = json.dumps(_payload("Edit", target))
    assert hooks.run_hook(hook_command, _ENV, io.StringIO(payload)) == 0


def test_the_first_edit_after_a_start_screen_animates_without_crashing(
    headless_nvim: str, tmp_path: Path
) -> None:
    FollowerState.set(
        _WINDOW, "nvim", headless_nvim, origin="", shown_any=False, adopted=False, speed="instant"
    )
    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.exec_lua("vim.cmd(...)", _START_SCREEN)
    start_screen = nvim.current.buffer
    assert start_screen.options["bufhidden"] == "wipe"
    target = (tmp_path / "file.py").resolve()
    target.write_text("a = 1\n")

    _run_hook("pre", target)
    target.write_text("a = 1\nb = 2\n")
    _run_hook("post", target)

    log = hooks.LOG_PATH.read_text() if hooks.LOG_PATH.exists() else ""
    assert "crashed" not in log, log
    assert nvim.current.buffer.name == str(target)
    assert nvim.current.buffer[:] == ["a = 1", "b = 2"]
    assert not nvim.api.buf_is_valid(start_screen.handle)  # its own bufhidden=wipe
