"""Real-nvim twin of tests/test_integration_crlf.py (Task 12 of
backlog-sweep-4, BACKLOG "CRLF files"): nvim loads a CRLF file with
fileformat=dos and shows its lines without the CRs, so an Edit of it is one
typed insert onto that buffer, which keeps 'fileformat' dos, and the file on
disk is never touched by the follower."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from test_integration_crlf import AFTER, BEFORE, _spy  # noqa: E402
from test_integration_edit_no_reload import _MARK, _last_state, _states  # noqa: E402
from test_nvim_integration_edit_base_mismatch import (  # noqa: E402
    _ENV,
    _OBSERVER_LUA,
    _WINDOW,
    _payload,
    _wait,
)

from vim_ai_follower import cache, hooks  # noqa: E402
from vim_ai_follower.diff import EditOp  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402

pytestmark = pytest.mark.integration


def _buffer(nvim: Any, name: str) -> Any:
    return next(b for b in nvim.buffers if b.name.endswith("/" + name))


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_an_edit_of_a_crlf_file_is_one_typed_insert(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adopted: bool
) -> None:
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set(_WINDOW, "nvim", headless_nvim, origin="", shown_any=False, adopted=adopted)
    log = tmp_path / "observer.log"
    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.exec_lua(_OBSERVER_LUA, str(log))
    target = (tmp_path / "a.py").resolve()
    if adopted:
        target.write_bytes(BEFORE)
        nvim.command(f"edit {target}")
    else:
        assert hooks.cmd_hook_pre(_ENV, _payload("Write", target)) == 0
        target.write_bytes(BEFORE)
        assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0
    assert _buffer(nvim, "a.py")[:] == ["a", "b"]
    assert _buffer(nvim, "a.py").options["fileformat"] == "dos"
    handle = _buffer(nvim, "a.py").number

    probes, scripts = _spy(monkeypatch)
    with log.open("a") as out:
        out.write(_MARK + "\n")
    assert hooks.cmd_hook_pre(_ENV, _payload("Edit", target)) == 0
    target.write_bytes(AFTER)
    written = target.stat().st_mtime_ns
    assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0

    assert _wait(lambda: _last_state(log, "a.py") == ["a", "X", "b"], timeout=5.0)
    assert probes == ["holds"]
    assert scripts == [[EditOp(kind="insert", start_line=2, end_line=1, new_lines=("X",))]]
    states = _states(log, "a.py")
    assert all(state[0] == "a" and state[-1] == "b" for state in states), states
    assert not any("\r" in line for state in states for line in state), states
    buffer = _buffer(nvim, "a.py")
    assert buffer.number == handle  # a diff, not a retype
    assert buffer[:] == ["a", "X", "b"]
    assert buffer.options["fileformat"] == "dos"
    assert target.read_bytes() == AFTER
    assert target.stat().st_mtime_ns == written

    # What a hand-off save writes: the dos buffer goes back as CRLF, so a
    # user who saves Claude's version saves exactly Claude's bytes.
    copy = tmp_path / "saved.py"
    nvim.exec_lua(
        "local buf, path = ...; vim.api.nvim_buf_call(buf, function()"
        " vim.cmd.write({ args = { path }, bang = true, mods = { silent = true } }) end)",
        buffer.number,
        str(copy),
    )
    assert copy.read_bytes() == AFTER
