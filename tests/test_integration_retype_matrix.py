"""Which touches retype the WHOLE file instead of typing only what changed.

Alberto (2026-10-20): "it seems the follower rewrites a whole file every time
it changes something; it should rewrite only what changes". This file
measures it from outside, through the real CLI, on the setup he saw it in
first: the follower's own nvim, split in tmux (a DEDICATED nvim).

An observer installed in the follower nvim (`nvim_buf_attach` on every
buffer, never a key sent) logs each `on_lines` event: the buffer's name, the
first changed row and the line count after the change. A touch that types
only the edit starts at the edited row; a whole-file retype starts at row 0
or empties the buffer. Each case writes its row to the test's report and to
`matrix.txt`, and asserts only that it ran to the end (exit 0, buffer ==
disk): the rows are the evidence the fixes start from."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from e2e_harness import E2EFollower, e2e_world, payload

pytestmark = pytest.mark.integration

LINE_COUNT = 200
EDITED_ROW = 149  # 0-based: the edit changes line 150 of 200

_OBSERVER_LUA = """
_G.vaf_observed = _G.vaf_observed or {}
local function watch(buffer)
  if _G.vaf_watched == nil then _G.vaf_watched = {} end
  if _G.vaf_watched[buffer] then return end
  -- An unloaded buffer refuses the attach (false): try again at its next event.
  _G.vaf_watched[buffer] = vim.api.nvim_buf_attach(buffer, false, {
    on_lines = function(_, changed, _, first, last, new_last)
      table.insert(_G.vaf_observed, {
        vim.api.nvim_buf_get_name(changed), first, last, new_last,
        vim.api.nvim_buf_line_count(changed), changed,
      })
    end,
    -- `:edit!` (show_fresh's read-then-clear, under noautocmd) reloads the
    -- buffer: no on_lines, the whole content is swapped.
    on_reload = function(_, changed)
      table.insert(_G.vaf_observed, {
        vim.api.nvim_buf_get_name(changed), 0, -1, -1,
        vim.api.nvim_buf_line_count(changed), changed,
      })
    end,
    on_detach = function(_, detached) _G.vaf_watched[detached] = nil end,
  })
end
for _, buffer in ipairs(vim.api.nvim_list_bufs()) do watch(buffer) end
if not _G.vaf_observer_autocmd then
  _G.vaf_observer_autocmd = vim.api.nvim_create_autocmd(
    {'BufNew', 'BufAdd', 'BufFilePre', 'BufEnter', 'BufWinEnter', 'TextChanged'}, {
    callback = function(arguments) watch(arguments.buf) end,
  })
end
_G.vaf_observed = {}
"""


@dataclass
class Touch:
    """What the observer saw for one file during one hook."""

    events: int
    first_row: int | None
    emptied: bool
    buffers: tuple[int, ...]

    @property
    def whole_file(self) -> bool:
        return self.emptied or self.first_row == 0


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _content(marker: str = "") -> str:
    return "".join(
        f"line {number}{marker if number == EDITED_ROW + 1 else ''}\n"
        for number in range(1, LINE_COUNT + 1)
    )


def _observe(world: E2EFollower, socket_path: str) -> None:
    world._nvim(socket_path).exec_lua(_OBSERVER_LUA, [])


def _typed_from_line(world: E2EFollower, socket_path: str, file_path: Path) -> Touch:
    """The observer's events for `file_path` since the last `_observe`."""
    wanted = file_path.resolve()
    nvim = world._nvim(socket_path)
    observed: list[Any] = nvim.exec_lua("return _G.vaf_observed", [])
    # A buffer the follower creates is filled before (or while) it is named,
    # so an event is the file's when its name then OR its buffer's name now
    # is the file.
    named_now: dict[int, str] = {
        int(handle): name
        for handle, name in nvim.exec_lua(
            "local names = {} for _, event in ipairs(_G.vaf_observed) do "
            "local handle = event[6] names[#names + 1] = {handle, "
            "vim.api.nvim_buf_is_valid(handle) and vim.api.nvim_buf_get_name(handle) or ''} "
            "end return names",
            [],
        )
    }

    def is_the_file(name: str) -> bool:
        return bool(name) and Path(name).resolve() == wanted

    mine = [
        event
        for event in observed
        if is_the_file(event[0]) or is_the_file(named_now[int(event[5])])
    ]
    return Touch(
        events=len(mine),
        first_row=min((event[1] for event in mine), default=None),
        emptied=any(event[4] <= 1 for event in mine),
        buffers=tuple(sorted({event[5] for event in mine})),
    )


def _state(world: E2EFollower) -> dict[str, Any]:
    data = json.loads((world.cache_dir / f"{world.window_id}.pane").read_text())
    assert isinstance(data, dict)
    return data


def _hook_log(world: E2EFollower) -> list[str]:
    path = world.cache_dir / "hook.log"
    return path.read_text(errors="replace").splitlines() if path.exists() else []


def _record(
    world: E2EFollower, case: str, touch: Touch, before: dict[str, Any], log_before: int
) -> str:
    row = (
        f"{case}: whole_file={touch.whole_file} first_row={touch.first_row} "
        f"emptied={touch.emptied} events={touch.events} buffers={touch.buffers} "
        f"open_before={len(before.get('open_files', []))} "
        f"stale_before={[Path(stale).name for stale in before.get('stale_files', [])]} "
        f"log={_hook_log(world)[log_before:]}"
    )
    report = Path(os.environ.get("VAF_RETYPE_MATRIX", str(world.workdir / "matrix.txt")))
    with report.open("a") as handle:
        handle.write(row + "\n")
    print(row)
    return row


def _edit(world: E2EFollower, file_path: Path, content: str) -> None:
    world.cli("hook", "pre", stdin=payload("Edit", file_path))
    file_path.write_text(content)
    world.cli("hook", "post", stdin=payload("Edit", file_path))


def _write(world: E2EFollower, file_path: Path, content: str) -> None:
    world.cli("hook", "pre", stdin=payload("Write", file_path))
    file_path.write_text(content)
    world.cli("hook", "post", stdin=payload("Write", file_path))


def _ends_equal_to_disk(world: E2EFollower, socket_path: str, file_path: Path) -> None:
    world.wait_until(
        lambda: world.nvim_buffer_bytes(socket_path, file_path) == file_path.read_bytes(),
        f"{file_path.name}'s buffer to match the disk",
        timeout=30.0,
    )


def _dedicated_nvim(world: E2EFollower) -> str:
    world.start("nvim", "muito_rapido")
    socket_path = world.follower_target()
    assert _state(world).get("adopted") is False
    return socket_path


def test_p1_an_edit_of_a_file_in_sync(world: E2EFollower) -> None:
    """The real bug if THIS retypes: an Edit of a file the follower shows,
    whose previous animation completed."""
    socket_path = _dedicated_nvim(world)
    target = world.workdir / "in_sync.py"
    _write(world, target, _content())
    _ends_equal_to_disk(world, socket_path, target)
    before, log_before = _state(world), len(_hook_log(world))
    _observe(world, socket_path)
    _edit(world, target, _content(" # edited"))
    _ends_equal_to_disk(world, socket_path, target)
    _record(
        world,
        "p1 nvim dedicated Edit in sync",
        _typed_from_line(world, socket_path, target),
        before,
        log_before,
    )


def _skip_an_edit_behind_a_busy_slot(world: E2EFollower, skipped: Path) -> dict[str, Any]:
    """Two hooks at once, as parallel subagent Edits do: a slow Edit holds
    the animating slot and an Edit of `skipped` finds it busy, is skipped and
    marks `skipped` stale. Returns the state after both."""
    slow = world.workdir / "slow.py"
    _write(world, slow, _content())
    _write(world, skipped, _content())
    for _ in range(4):
        world.cli("speed-down")
    world.cli("hook", "pre", stdin=payload("Edit", slow))
    slow.write_text("".join(f"changed {number}\n" for number in range(1, 41)) + _content()[1000:])
    running = world.cli_background("hook", "post", stdin=payload("Edit", slow))
    world.wait_for_animating("running")
    _edit(world, skipped, _content(" # skipped edit"))
    world.wait_for_hook_exit(running)
    for _ in range(4):
        world.cli("speed-up")
    state = _state(world)
    stale = state.get("stale_files", [])
    assert str(skipped) in stale or str(skipped.resolve()) in stale, state
    return state


def test_p2_an_edit_after_a_busy_slot_skip(world: E2EFollower) -> None:
    """A third Edit of the file a busy slot made stale."""
    socket_path = _dedicated_nvim(world)
    skipped = world.workdir / "skipped.py"
    before = _skip_an_edit_behind_a_busy_slot(world, skipped)
    log_before = len(_hook_log(world))
    _observe(world, socket_path)
    _edit(world, skipped, _content(" # third edit"))
    _ends_equal_to_disk(world, socket_path, skipped)
    _record(
        world,
        "p2 nvim dedicated Edit after busy skip",
        _typed_from_line(world, socket_path, skipped),
        before,
        log_before,
    )


def test_p3_an_edit_after_max_tabs_eviction(world: E2EFollower) -> None:
    """Six files with max_tabs 5, then back to the first (evicted)."""
    socket_path = _dedicated_nvim(world)
    files = [world.workdir / f"file_{number}.py" for number in range(1, 7)]
    for file_path in files:
        _write(world, file_path, _content())
    _ends_equal_to_disk(world, socket_path, files[-1])
    before, log_before = _state(world), len(_hook_log(world))
    _observe(world, socket_path)
    _edit(world, files[0], _content(" # back to the first"))
    _ends_equal_to_disk(world, socket_path, files[0])
    _record(
        world,
        "p3 nvim dedicated Edit after eviction",
        _typed_from_line(world, socket_path, files[0]),
        before,
        log_before,
    )


def test_p4_the_first_touch_of_a_file(world: E2EFollower) -> None:
    """Nothing is shown yet: a full type is required."""
    socket_path = _dedicated_nvim(world)
    target = world.workdir / "first.py"
    target.write_text(_content())
    before, log_before = _state(world), len(_hook_log(world))
    _observe(world, socket_path)
    _edit(world, target, _content(" # first touch"))
    _ends_equal_to_disk(world, socket_path, target)
    touch = _typed_from_line(world, socket_path, target)
    _record(world, "p4 nvim dedicated first touch", touch, before, log_before)
    # The instrument's canary: a first touch has no buffer to diff, so the
    # observer MUST see it typed from the top, or it cannot see a retype.
    assert touch.whole_file, touch


def _adopt(world: E2EFollower) -> None:
    """Make the running follower read as ADOPTED (the user's own editor),
    as tests/test_e2e_cli_controls.py does: only the state's flag differs."""
    state_file = world.cache_dir / f"{world.window_id}.pane"
    state = json.loads(state_file.read_text())
    state["adopted"] = True
    state_file.write_text(json.dumps(state))


def _measure_nvim(
    world: E2EFollower, socket_path: str, case: str, target: Path, change: Callable[[], None]
) -> Touch:
    before, log_before = _state(world), len(_hook_log(world))
    _observe(world, socket_path)
    change()
    _ends_equal_to_disk(world, socket_path, target)
    touch = _typed_from_line(world, socket_path, target)
    _record(world, case, touch, before, log_before)
    return touch


def test_s4_an_edit_after_a_read(world: E2EFollower) -> None:
    socket_path = _dedicated_nvim(world)
    target = world.workdir / "read_first.py"
    _write(world, target, _content())
    world.cli("hook", "post", stdin=payload("Read", target))
    _measure_nvim(
        world,
        socket_path,
        "s4 nvim dedicated Edit after Read",
        target,
        lambda: _edit(world, target, _content(" # after read")),
    )


def test_s5_a_write_that_changes_one_line(world: E2EFollower) -> None:
    socket_path = _dedicated_nvim(world)
    target = world.workdir / "rewritten.py"
    _write(world, target, _content())
    _ends_equal_to_disk(world, socket_path, target)
    _measure_nvim(
        world,
        socket_path,
        "s5 nvim dedicated Write of one changed line",
        target,
        lambda: _write(world, target, _content(" # written")),
    )


def test_s6_pre_and_post_on_different_pane_identities(world: E2EFollower) -> None:
    """The pre hook knows its pane; the post hook lost TMUX_PANE (the
    binding path carries it)."""
    socket_path = _dedicated_nvim(world)
    target = world.workdir / "rebound.py"
    _write(world, target, _content())
    _ends_equal_to_disk(world, socket_path, target)

    def change() -> None:
        world.cli("hook", "pre", stdin=payload("Edit", target))
        target.write_text(_content(" # rebound"))
        world.cli(
            "hook",
            "post",
            stdin=payload("Edit", target),
            env=world.env_with(drop=("TMUX_PANE",)),
        )

    _measure_nvim(
        world, socket_path, "s6 nvim dedicated Edit, post without TMUX_PANE", target, change
    )


def test_s7_an_edit_after_a_handed_over_save(world: E2EFollower) -> None:
    """An animation interrupted, its buffer saved by the user (`:w`), then
    the next Edit."""
    socket_path = _dedicated_nvim(world)
    target = world.workdir / "handed.py"
    _write(world, target, _content())
    _ends_equal_to_disk(world, socket_path, target)
    for _ in range(4):
        world.cli("speed-down")
    world.cli("hook", "pre", stdin=payload("Edit", target))
    target.write_text("".join(f"changed {number}\n" for number in range(1, 41)) + _content()[1000:])
    running = world.cli_background("hook", "post", stdin=payload("Edit", target))
    world.wait_for_animating("running")
    world.cli("interrupt")
    world.wait_for_handoff()
    for _ in range(4):
        world.cli("speed-up")
    nvim = world._nvim(socket_path)
    for buffer in nvim.buffers:
        if buffer.name and Path(buffer.name).resolve() == target.resolve():
            nvim.command(f"buffer {buffer.number} | write!")
    world.wait_for_hook_exit(running, timeout=30.0)
    _measure_nvim(
        world,
        socket_path,
        "s7 nvim dedicated Edit after hand-off save",
        target,
        lambda: _edit(
            world, target, target.read_text().replace("line 150\n", "line 150 # after\n")
        ),
    )


def test_s2_adopted_an_edit_after_a_busy_slot_skip(world: E2EFollower) -> None:
    """The same stale file in an ADOPTED nvim: the probe decides."""
    socket_path = _dedicated_nvim(world)
    _adopt(world)
    skipped = world.workdir / "skipped.py"
    before = _skip_an_edit_behind_a_busy_slot(world, skipped)
    log_before = len(_hook_log(world))
    _observe(world, socket_path)
    _edit(world, skipped, _content(" # third edit"))
    world.wait_until(lambda: world.animating_state() is None, "the hook to finish", timeout=30.0)
    _record(
        world,
        "s2 nvim adopted Edit after busy skip",
        _typed_from_line(world, socket_path, skipped),
        before,
        log_before,
    )


# A Vim timer that logs the buffer whenever it changes, never a key sent
# (tests/test_integration_edit_no_reload.py's observer).
_VIM_OBSERVER = r"""
let g:vaf_observed_last = ''
function! VafObserverTick(timer) abort
  let l:state = json_encode({'name': expand('%:t'), 'lines': getline(1, '$')})
  if l:state !=# g:vaf_observed_last
    let g:vaf_observed_last = l:state
    call writefile([l:state], g:vaf_observer_log, 'a')
  endif
endfunction
call timer_start(15, 'VafObserverTick', {'repeat': -1})
"""


def _dedicated_vim(world: E2EFollower) -> Path:
    log = world.workdir / "vim-observer.log"
    world.set_vimrc(f"let g:vaf_observer_log = '{log}'\n{_VIM_OBSERVER}")
    world.start("tmux", "muito_rapido")
    assert _state(world).get("adopted") is False
    world.wait_until(log.exists, "the Vim observer to start", timeout=10.0)
    return log


def _vim_states(log: Path, name: str, since: int) -> list[list[str]]:
    states: list[list[str]] = []
    for raw in log.read_text().splitlines()[since:]:
        entry = json.loads(raw)
        if entry["name"] == name:
            states.append(entry["lines"])
    return states


def vim_touch(states: list[list[str]], base: list[str]) -> Touch:
    """Where each logged frame first differs from the one before it,
    starting from the buffer's content before the hook."""
    first_rows: list[int] = []
    previous = base
    for lines in states:
        row = next(
            (
                index
                for index, (old, new) in enumerate(zip(previous, lines, strict=False))
                if old != new
            ),
            min(len(previous), len(lines)),
        )
        if lines != previous:
            first_rows.append(row)
        previous = lines
    return Touch(
        events=len(states),
        first_row=min(first_rows, default=None),
        emptied=any(len(lines) <= 1 for lines in states),
        buffers=(),
    )


def _measure_vim(
    world: E2EFollower, log: Path, case: str, target: Path, change: Callable[[], None]
) -> Touch:
    before, log_before = _state(world), len(_hook_log(world))
    base = target.read_text().splitlines()
    since = len(log.read_text().splitlines())
    change()
    expected = target.read_text().splitlines()
    world.wait_until(
        lambda: (_vim_states(log, target.name, since) or [[]])[-1] == expected,
        f"{target.name}'s Vim buffer to match the disk",
        timeout=30.0,
    )
    states = _vim_states(log, target.name, since)
    # The instrument saw the animation, not only its end.
    assert len(states) >= 2, states[-1:]
    touch = vim_touch(states, base)
    _record(world, case, touch, before, log_before)
    return touch


def test_vim_s1_an_edit_of_a_file_in_sync(world: E2EFollower) -> None:
    log = _dedicated_vim(world)
    target = world.workdir / "in_sync.py"
    _write(world, target, _content())
    world.wait_until(
        lambda: (_vim_states(log, target.name, 0) or [[]])[-1] == _content().splitlines(),
        "the first Write to land",
        timeout=30.0,
    )
    _measure_vim(
        world,
        log,
        "vim s1 tmux dedicated Edit in sync",
        target,
        lambda: _edit(world, target, _content(" # edited")),
    )


def test_vim_s2_an_edit_after_a_busy_slot_skip(world: E2EFollower) -> None:
    log = _dedicated_vim(world)
    skipped = world.workdir / "skipped.py"
    _skip_an_edit_behind_a_busy_slot(world, skipped)
    world.wait_until(lambda: world.animating_state() is None, "the hooks to finish", timeout=30.0)
    _measure_vim(
        world,
        log,
        "vim s2 tmux dedicated Edit after busy skip",
        skipped,
        lambda: _edit(world, skipped, _content(" # third edit")),
    )


def test_the_harness_can_tell_a_diff_from_a_retype() -> None:
    """The instrument's own check: a Touch from row 149 is a diff, one from 0
    or one that emptied the buffer is a retype."""
    assert not Touch(events=1, first_row=EDITED_ROW, emptied=False, buffers=(1,)).whole_file
    assert Touch(events=1, first_row=0, emptied=False, buffers=(1,)).whole_file
    assert Touch(events=2, first_row=EDITED_ROW, emptied=True, buffers=(1,)).whole_file
    base = ["a", "b", "c"]
    assert vim_touch([["a", "b", "cx"], ["a", "b", "cxy"]], base).first_row == 2
    assert vim_touch([[""], ["a"], ["a", "b"]], base).whole_file
