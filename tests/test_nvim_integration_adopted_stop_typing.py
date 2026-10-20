"""`claude-follow stop` of an ADOPTED nvim whose user is in the middle of
something closes the follower's tabs the user is not in, and nothing else.

Measured 2026-10-18 (T24c item 6) on a real TUI nvim in a tmux pane, a second
RPC connection playing the teardown (the status float's clear, then the
adopted eviction of every follower tab outside the user's): in insert,
replace, visual, select and terminal mode, at a confirm (`:s///gc`), on the
command line and with an operator pending, the user's mode, tab, window,
buffer, cursor, visual start and half-typed command line read the same after
it as before, and the keys they typed next finished their work in their own
buffer. So stop closes those tabs (it used to send nothing at all, leaving
every follower tab open). A follower buffer with a window in the user's
CURRENT tab is left alone, with one hook.log line: closing a window there can
move the user. Still nothing is sent in the command-line window (the wipe is
refused with E11 behind `silent!`, and the eviction reported "wiped" anyway)
or with keys pending (every request waits until the user finishes).

Real CLI, real tmux, real nvims in tmux panes, `HOME` isolated by the e2e
harness; every CLI run has a subprocess timeout so a regression FAILS."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pynvim
import pytest
from e2e_harness import E2EFollower, e2e_world
from test_nvim_integration_adopted_prompt import _hook_log, _nvim_window, _timed_cli

from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


class _Adopted:
    """The user's nvim with their file in tab 1 and a clean buffer the
    follower created for each of `claudes_files` in a tab of its own."""

    def __init__(self, world: E2EFollower, claudes_files: tuple[str, ...]) -> None:
        self.world = world
        self.socket_path = world.cache_dir / "nvim-adopted.sock"
        world.cache_dir.mkdir(parents=True, exist_ok=True)
        self.users_file = world.workdir / "a.txt"
        self.users_file.write_text("u1\nu2\nu3\n")
        self.claudes_paths = tuple(os.path.realpath(world.workdir / name) for name in claudes_files)
        self.window_id, self.pane_id = _nvim_window(world, self.socket_path, self.users_file)
        nvim = self.nvim()
        self.users_tab = nvim.api.get_current_tabpage().handle
        self.claudes_buffers: list[int] = []
        for path in self.claudes_paths:
            Path(path).write_text("claude\n")
            nvim.command("tabnew")
            buffer = nvim.api.create_buf(True, False)
            nvim.api.buf_set_name(buffer, path)
            nvim.api.buf_set_lines(buffer, 0, -1, True, ["claude"])
            nvim.api.buf_set_option(buffer, "modified", False)
            nvim.api.buf_set_var(buffer, "vaf_created", 1)
            nvim.api.win_set_buf(0, buffer)
            self.claudes_buffers.append(buffer.handle)
        nvim.api.set_current_tabpage(nvim.api.list_tabpages()[0])
        FollowerState.set(
            self.window_id,
            "nvim",
            str(self.socket_path),
            origin=self.pane_id,
            server_pid=world.server_pid,
            adopted=True,
            shown_any=True,
            open_files=self.claudes_paths,
            speed="rapido",
            base_directory=world.cache_dir,
        )

    def nvim(self) -> pynvim.Nvim:
        return self.world._nvim(str(self.socket_path))

    def keys(self, *keys: str) -> None:
        self.world.tmux("send-keys", "-t", self.pane_id, *keys)

    def wait_for_mode(self, mode: str) -> None:
        self.world.wait_until(
            lambda: self.nvim().api.get_mode() == {"mode": mode, "blocking": False},
            f"the user's nvim in mode {mode}",
            timeout=10.0,
        )

    def where_the_user_is(self) -> dict[str, object]:
        nvim = self.nvim()
        return {
            "mode": nvim.api.get_mode(),
            "tab": nvim.api.get_current_tabpage().handle,
            "window": nvim.api.get_current_win().handle,
            "buffer": nvim.api.get_current_buf().handle,
            "cursor": nvim.api.win_get_cursor(0),
            "visual start": nvim.call("getpos", "v"),
            "command line": nvim.call("getcmdline"),
        }

    def stop(self) -> None:
        result, seconds = _timed_cli(self.world, "stop", pane_id=self.pane_id)
        assert result.returncode == 0, result.stderr
        assert seconds < 15.0, f"stop took {seconds:.1f} s"

    def users_lines(self) -> list[str]:
        nvim = self.nvim()
        lines: list[str] = nvim.api.buf_get_lines(
            nvim.call("bufnr", str(self.users_file)), 0, -1, True
        )
        return lines


# (keys that enter it, the mode, keys that finish it, the user's file after)
_MODES: dict[
    str, tuple[tuple[tuple[str, ...], ...], str, tuple[tuple[str, ...], ...], list[str]]
] = {
    "insert": (
        (("A",), ("-l", "half")),
        "i",
        (("-l", "MORE"), ("Escape",)),
        ["u1halfMORE", "u2", "u3"],
    ),
    "replace": (
        (("R",), ("-l", "half")),
        "R",
        (("-l", "MORE"), ("Escape",)),
        ["halfMORE", "u2", "u3"],
    ),
    "visual": ((("v", "l"),), "v", (("d",),), ["", "u2", "u3"]),
    "select": ((("g", "h", "Right"),), "s", (("-l", "X"), ("Escape",)), ["X", "u2", "u3"]),
    "confirm": (((":%s/u/Z/gc", "Enter"),), "r?", (("a",),), ["Z1", "Z2", "Z3"]),
    "command line": (((":let g:who = bufname('%')",),), "c", (("Enter",),), ["u1", "u2", "u3"]),
    "operator pending": ((("d",),), "no", (("d",),), ["u2", "u3"]),
    "terminal": ((("i",),), "t", (("-l", "echo hi$((1+1))"), ("Enter",)), ["u1", "u2", "u3"]),
}


@pytest.mark.parametrize("mode_name", list(_MODES))
def test_stop_closes_the_follower_tabs_a_busy_user_is_not_in(
    world: E2EFollower, mode_name: str
) -> None:
    enter, mode, finish, users_lines_after = _MODES[mode_name]
    adopted = _Adopted(world, ("claude.py",))
    if mode_name == "terminal":
        adopted.nvim().command("terminal")
    for keys in enter:
        adopted.keys(*keys)
    adopted.wait_for_mode(mode)
    time.sleep(0.3)  # let the screen settle before the reading below
    before = adopted.where_the_user_is()

    adopted.stop()

    nvim = adopted.nvim()
    assert len(nvim.api.list_tabpages()) == 1, "the follower's tab was left open"
    assert not nvim.api.buf_is_valid(adopted.claudes_buffers[0])
    assert adopted.where_the_user_is() == before
    for keys in finish:
        adopted.keys(*keys)
    if mode_name == "terminal":
        world.wait_until(
            lambda: any("hi2" in line for line in adopted.nvim().api.buf_get_lines(0, 0, -1, True)),
            "the user's command to run in their terminal",
            timeout=10.0,
        )
    else:
        world.wait_until(
            lambda: adopted.nvim().api.get_mode()["mode"] == "n", "the user's work to finish"
        )
    assert adopted.users_lines() == users_lines_after
    if mode_name == "command line":
        assert os.path.realpath(adopted.nvim().eval("g:who")) == os.path.realpath(
            adopted.users_file
        )
    log = _hook_log(world)
    assert "Traceback" not in log, log
    waiting = [line for line in log.splitlines() if "waiting on its user" in line]
    assert len(waiting) == 1 and "closing only the follower's tabs it is not in" in waiting[0], log


def test_stop_leaves_a_follower_tab_the_busy_user_is_in(world: E2EFollower) -> None:
    # The user typing in a split of the follower's first tab: its window
    # stays, with one hook.log line; the follower's other tab closes.
    adopted = _Adopted(world, ("claude.py", "other.py"))
    nvim = adopted.nvim()
    nvim.api.set_current_tabpage(nvim.api.list_tabpages()[1])
    nvim.command(f"vsplit {adopted.users_file}")
    adopted.keys("A")
    adopted.keys("-l", "half")
    adopted.wait_for_mode("i")
    before = adopted.where_the_user_is()

    adopted.stop()

    nvim = adopted.nvim()
    assert len(nvim.api.list_tabpages()) == 2
    assert nvim.api.buf_is_valid(adopted.claudes_buffers[0])
    assert not nvim.api.buf_is_valid(adopted.claudes_buffers[1])
    assert adopted.where_the_user_is() == before
    adopted.keys("-l", "MORE")
    adopted.keys("Escape")
    world.wait_until(lambda: adopted.nvim().api.get_mode()["mode"] == "n", "insert mode to end")
    assert adopted.users_lines() == ["u1halfMORE", "u2", "u3"]
    log = _hook_log(world)
    current = [line for line in log.splitlines() if "the tab the user is in" in line]
    assert len(current) == 1 and adopted.claudes_paths[0] in current[0], log


@pytest.mark.parametrize(("inside", "mode"), [("", "n"), ("i", "i"), ("v", "v")])
def test_stop_still_sends_nothing_in_the_command_line_window(
    world: E2EFollower, inside: str, mode: str
) -> None:
    # Whatever mode the user is in inside it: `q:` then `i` read "in insert
    # mode", and the teardown ran there (review m1 of T24c).
    adopted = _Adopted(world, ("claude.py",))
    adopted.keys("q:")
    world.wait_until(
        lambda: adopted.nvim().call("getcmdwintype") == ":", "the command-line window", timeout=10.0
    )
    if inside:
        adopted.keys(inside)
    adopted.wait_for_mode(mode)
    try:
        adopted.stop()
        nvim = adopted.nvim()
        assert len(nvim.api.list_tabpages()) == 2
        assert nvim.api.buf_is_valid(adopted.claudes_buffers[0])
        assert nvim.call("getcmdwintype") == ":"
        assert nvim.api.get_mode()["mode"] == mode
        log = _hook_log(world)
        waiting = [line for line in log.splitlines() if "waiting on its user" in line]
        assert len(waiting) == 1 and "(in the command-line window)" in waiting[0], log
        assert "left open" in waiting[0], log
    finally:
        adopted.keys("C-c")
        adopted.keys("Escape")
