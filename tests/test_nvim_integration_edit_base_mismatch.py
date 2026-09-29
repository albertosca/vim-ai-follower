"""The base-mismatch guard on the nvim backend, watched mid-animation.

nvim's twin of tests/test_integration_edit_base_mismatch.py. The observer is
a libuv timer inside the headless nvim itself (installed over RPC, 15 ms, logs
every distinct state of the current buffer to the same JSON-lines format the
Vim observer writes), so the tmux tests' _states/_last_state helpers read it
unchanged. It runs on nvim's own event loop between the hook's RPC calls and
never touches the buffer. A missed sample can hide a violation, never invent
one.

The hooks run standalone (no tmux), the way
test_nvim_integration.test_standalone_hook_post_edit_animates_a_headless_nvim_end_to_end
does, so the real session resolution, snapshot and FollowerState paths run.

Case (1) needs a CLEAN buffer to unload: every buffer the nvim follower TYPED
is modified (it is never written), and `:tabclose` under 'nohidden' keeps a
modified buffer loaded. A buffer a Read opened from disk is clean, so that is
the one a user closing its tab can unload.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from test_integration_edit_base_mismatch import (  # noqa: E402
    FORMATTED,
    FORMATTED_THREE,
    STALE_LINE,
)
from test_integration_edit_no_reload import (  # noqa: E402
    _MARK,
    ONE_DEF,
    OTHER,
    THREE_DEFS,
    _defs,
    _last_state,
    _states,
)

from vim_ai_follower import cache, hooks  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402

pytestmark = pytest.mark.integration

_WINDOW = "term-b2"  # session._standalone_id("b2")
_ENV = {"TERM_SESSION_ID": "b2"}

_OBSERVER_LUA = """
local log = ...
local last = ''
local timer = vim.uv.new_timer()
timer:start(0, 15, vim.schedule_wrap(function()
  local buf = vim.api.nvim_get_current_buf()
  local state = vim.json.encode({
    name = vim.fn.fnamemodify(vim.api.nvim_buf_get_name(buf), ':t'),
    lines = vim.api.nvim_buf_get_lines(buf, 0, -1, false),
  })
  if state ~= last then
    last = state
    local f = io.open(log, 'a')
    f:write(state .. '\\n')
    f:close()
  end
end))
"""


def _payload(tool: str, target: Path) -> dict[str, Any]:
    return {"tool_name": tool, "tool_input": {"file_path": str(target)}}


def _observed_nvim(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Any]:
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set(_WINDOW, "nvim", headless_nvim, origin="", shown_any=False)
    log = tmp_path / "observer.log"
    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.exec_lua(_OBSERVER_LUA, str(log))
    return log, nvim


def _wait(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _edit(log: Path, target: Path, content: str) -> list[list[str]]:
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    assert hooks.cmd_hook_pre(_ENV, _payload("Edit", target)) == 0
    target.write_text(content)
    assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0
    after = content.splitlines()
    # The observer samples asynchronously; let it log the final frame. nvim
    # never re-reads disk after an animation (tmux's relock `:e!` does), so a
    # diff typed onto the wrong base stays wrong here: the end state is a
    # symptom too.
    settled = _wait(lambda: _last_state(log, target.name) == after, timeout=5.0)
    final = _last_state(log, target.name) or []
    assert settled, (
        f"the buffer never reached the edited file: defs={_defs(final)} "
        f"stale={STALE_LINE in final} lines={final!r}"
    )
    return _states(log, target.name)


def test_edit_after_an_outside_rewrite_is_retyped_not_typed_onto_the_stale_buffer(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, _ = _observed_nvim(headless_nvim, tmp_path, monkeypatch)
    target = (tmp_path / "a.py").resolve()
    target.write_text(ONE_DEF)
    assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0
    target.write_text(FORMATTED)  # a formatter, not Claude

    states = _edit(log, target, FORMATTED_THREE)
    after = FORMATTED_THREE.splitlines()
    trace = "\n".join(f"defs={_defs(s)} lines={len(s)} stale={STALE_LINE in s}" for s in states)
    assert len(states) >= 2, f"observer caught no intermediate state:\n{trace}"
    mixed = [s for s in states if STALE_LINE in s and any("def bravo" in line for line in s)]
    assert not mixed, f"the diff was typed onto the stale buffer:\n{trace}"
    assert after not in states[:-1], f"finished file shown before the animation ended:\n{trace}"


def test_edit_of_a_read_file_whose_tab_was_closed_under_nohidden_is_retyped(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, nvim = _observed_nvim(headless_nvim, tmp_path, monkeypatch)
    target = (tmp_path / "a.py").resolve()
    target.write_text(ONE_DEF)
    other = (tmp_path / "b.py").resolve()
    other.write_text(OTHER)
    # Both opened by a Read: clean, disk-backed buffers, a.py in tab 2.
    assert hooks.cmd_hook_post(_ENV, _payload("Read", target)) == 0
    assert hooks.cmd_hook_post(_ENV, _payload("Read", other)) == 0
    a_tab = next(
        tab.number
        for tab in nvim.tabpages
        if any(win.buffer.name.endswith("/a.py") for win in tab.windows)
    )
    nvim.command("set nohidden")
    nvim.command(f"{a_tab}tabclose")
    a_buf = next(b for b in nvim.buffers if b.name.endswith("/a.py"))
    assert nvim.api.buf_is_loaded(a_buf.number) is False, "setup: a.py should be unloaded"

    states = _edit(log, target, THREE_DEFS)
    after = THREE_DEFS.splitlines()
    trace = "\n".join(f"defs={_defs(s)} lines={len(s)}" for s in states)
    assert len(states) >= 2, f"observer caught no intermediate state:\n{trace}"
    assert after not in states[:-1], f"finished file shown before the animation ended:\n{trace}"
    assert max(_defs(s) for s in states) <= _defs(after), f"duplicated defs:\n{trace}"


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_a_read_landing_between_an_edits_pre_and_post_hooks_never_flashes_the_finished_file(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adopted: bool
) -> None:
    """nvim's twin of the tmux test of the same name (BACKLOG D2): another
    agent's Read of the file posts after this Edit wrote it and before its
    post hook. The Read used to re-read the clean buffer (C5's
    _reload_if_clean), putting the finished file on screen; the Edit's probe
    then saw a buffer that was not its base — a dedicated follower wiped and
    retyped it, an adopted one left it alone with the "buffer differs" cue
    and never animated the edit at all."""
    log, _ = _observed_nvim(headless_nvim, tmp_path, monkeypatch)
    if adopted:
        FollowerState.update(_WINDOW, adopted=True)
    target = (tmp_path / "a.py").resolve()
    target.write_text(ONE_DEF)
    # Opened by a Read: a clean, disk-backed buffer holding the edit's base.
    assert hooks.cmd_hook_post(_ENV, _payload("Read", target)) == 0
    assert _wait(lambda: _last_state(log, target.name) == ONE_DEF.splitlines(), timeout=5.0)

    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    assert hooks.cmd_hook_pre(_ENV, _payload("Edit", target)) == 0
    target.write_text(THREE_DEFS)
    assert hooks.cmd_hook_post(_ENV, _payload("Read", target)) == 0  # the other agent
    time.sleep(0.2)  # a reload, if any, gets its own observer frame
    assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0

    after = THREE_DEFS.splitlines()
    settled = _wait(lambda: _last_state(log, target.name) == after, timeout=5.0)
    states = _states(log, target.name)
    trace = "\n".join(f"defs={_defs(s)} lines={len(s)}" for s in states)
    assert settled, f"the buffer never reached the edited file:\n{trace}"
    assert len(states) >= 2, f"observer caught no intermediate state:\n{trace}"
    assert after not in states[:-1], f"finished file shown before the animation ended:\n{trace}"
    assert min(len(s) for s in states) >= len(ONE_DEF.splitlines()), f"retyped:\n{trace}"
