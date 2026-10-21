"""An adopted nvim whose user opens the command-line window (`q:`) AFTER the
hook's probe found them idle (sweep-4 residual of T24c). Every window change
in the cmdwin raises E11, so whatever the follower does next must not crash
the hook, half-make a tab or touch a buffer: it skips in one log line and
marks the file stale, as when the probe itself sees the cmdwin.

The race is made deterministic: `hooks.adopted_user_pending` is wrapped so
that, once the real probe has answered "nothing pending", the test opens the
cmdwin and waits for it before the hook goes on."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from test_nvim_integration_edit_base_mismatch import _ENV, _WINDOW, _payload  # noqa: E402

from vim_ai_follower import cache, hooks  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402

pytestmark = pytest.mark.integration


def _cmdwin_after_the_probe(monkeypatch: pytest.MonkeyPatch, socket_path: str) -> list[int]:
    """Open the cmdwin right after each probe that found nothing pending;
    returns the list the wrapper appends to once per cmdwin it opened."""
    real: Callable[[str], str | None] = hooks.adopted_user_pending
    opened: list[int] = []

    def probe_then_open(probed: str) -> str | None:
        answer = real(probed)
        if answer is None and not opened:
            user = pynvim.attach("socket", path=socket_path)
            try:
                user.input("q:")
                for _attempt in range(200):
                    if user.call("getcmdwintype") == ":":
                        break
                    time.sleep(0.01)
                assert user.call("getcmdwintype") == ":"
            finally:
                user.close()
            opened.append(1)
        return answer

    monkeypatch.setattr(hooks, "adopted_user_pending", probe_then_open)
    return opened


def _adopted(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, open_files: tuple[str, ...]
) -> Any:
    monkeypatch.setattr(cache, "CACHE_DIRECTORY", tmp_path / "cache")
    FollowerState.set(
        _WINDOW,
        "nvim",
        headless_nvim,
        origin="",
        shown_any=True,
        adopted=True,
        open_files=open_files,
    )
    return pynvim.attach("socket", path=headless_nvim)


@pytest.mark.parametrize("tool", ["Edit", "Edit-first-touch", "Read"])
def test_a_cmdwin_opened_after_the_probe_is_not_acted_on(
    headless_nvim: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tool: str,
) -> None:
    users_file = (tmp_path / "a.txt").resolve()
    users_file.write_text("aaa\n")
    claudes_file = (tmp_path / "file.py").resolve()
    claudes_file.write_text("one\ntwo\n")
    already_open = tool == "Edit"
    nvim = _adopted(
        headless_nvim,
        tmp_path,
        monkeypatch,
        (str(claudes_file),) if already_open else (),
    )
    try:
        claudes_buffer = -1
        if already_open:
            nvim.command(f"edit {claudes_file}")
            claudes_buffer = nvim.call("bufnr", str(claudes_file))
        nvim.command(f"edit {users_file}")
        hook_tool = "Read" if tool == "Read" else "Edit"
        payload = _payload(hook_tool, claudes_file)
        if hook_tool == "Edit":
            assert hooks.cmd_hook_pre(_ENV, payload) == 0
            claudes_file.write_text("one\nTWO\n")
        buffers_before = len(nvim.call("getbufinfo"))
        tabs_before = len(nvim.api.list_tabpages())
        opened = _cmdwin_after_the_probe(monkeypatch, headless_nvim)

        with caplog.at_level(logging.INFO):
            assert hooks.cmd_hook_post(_ENV, payload) == 0

        assert opened == [1], "the race was never set up"
        assert "Traceback" not in caplog.text, caplog.text
        assert not any(record.exc_info for record in caplog.records), caplog.text
        assert nvim.call("getcmdwintype") == ":"
        assert len(nvim.api.list_tabpages()) == tabs_before
        if already_open:
            assert nvim.api.buf_get_lines(claudes_buffer, 0, -1, False) == ["one", "two"]
        else:
            # A buffer the follower made before the E11 cannot be wiped in
            # the cmdwin: it is unlisted at once and wiped on CmdwinLeave.
            assert nvim.call("buflisted", str(claudes_file)) == 0
        skipped = [
            record.getMessage()
            for record in caplog.records
            if "command-line window" in record.getMessage()
        ]
        assert len(skipped) == 1, caplog.text
        state = FollowerState.get(_WINDOW)
        assert state is not None
        if hook_tool == "Edit":
            assert state.stale_files == (os.path.realpath(claudes_file),)

        nvim.input("<C-c>")
        for _attempt in range(200):
            if nvim.call("getcmdwintype") == "":
                break
            time.sleep(0.01)
        nvim.command("sleep 50m")  # the scheduled wipe runs after CmdwinLeave
        # The cmdwin's own buffer went with it: only the user's buffers stay.
        assert len(nvim.call("getbufinfo")) == buffers_before
        if not already_open:
            assert nvim.call("bufnr", str(claudes_file)) == -1
    finally:
        nvim.input("<C-c>")
        nvim.close()
