"""An ADOPTED editor keeps swap files on the buffers the follower creates or
loads, with no prompt: a real UI nvim in a tmux pane and a real Vim at 49
columns, through the real CLI.

Why swap is turned off at all: naming or loading a buffer for a file another
editor holds runs the swap check, and E325 ATTENTION either raised out of the
call or blocked the follower's editor at a pager. So the follower's buffer
opts out for that step. A DEDICATED follower stays swap-off (its buffers are
display-only). An adopted editor is the user's own, and without swap a crash
loses their typing and a second editor opening the file gets no warning
(Alberto's ruling, 2026-09-28), so the swap is turned back on right after.

Turning it back on runs the same check. Measured 2026-09-28
(`scratchpad/sweep3/B2-reg3-variants.txt`, 16 cases): a plain
`setlocal swapfile` raised E325 in nvim and left the ATTENTION block at
`-- More --` in a 49-column Vim; a scoped SwapExists answer does NOT help
(SwapExists never fires for an option toggle, 0 in every case); a scoped
`shortmess+=A` (restored in `finally`) was silent in both editors, created
the swap (`.swo` beside a held one, `.swp` otherwise), and a second editor
opening the file afterwards still got its SwapExists."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from e2e_harness import E2EFollower, e2e_world, payload

pytestmark = pytest.mark.integration


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _adopt(world: E2EFollower) -> None:
    """The one thing the hooks read to tell adopted from dedicated."""
    state_file = world.cache_dir / f"{world.window_id}.pane"
    state = json.loads(state_file.read_text())
    state["adopted"] = True
    state_file.write_text(json.dumps(state))


def _write(world: E2EFollower, path: Path, content: str) -> None:
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text(content)
    result = world.cli("hook", "post", stdin=payload("Write", path))
    assert result.stderr == ""


def _clean_log(world: E2EFollower) -> None:
    log = (world.cache_dir / "hook.log").read_text().lower()
    for needle in ("traceback", "error", "e325"):
        assert needle not in log, f"{needle!r} in hook.log:\n{log}"


def _screen_is_clean(world: E2EFollower, pane: str) -> None:
    screen = world.capture_pane(pane)
    for needle in ("ATTENTION", "-- More --", "Press ENTER", "E325"):
        assert needle not in screen, f"{needle!r} on the follower's screen:\n{screen}"


# ---------------------------------------------------------------- nvim


def _nvim_swap_dir(world: E2EFollower) -> Path:
    return world.home / ".local" / "state" / "nvim" / "swap"


def _open_owner_nvim(world: E2EFollower, target: Path) -> None:
    world.tmux("new-window", "-d", "-t", world.session, f"nvim -u NONE -i NONE {target}")
    swap_dir = _nvim_swap_dir(world)
    world.wait_until(
        lambda: swap_dir.exists() and any(target.name in p.name for p in swap_dir.iterdir()),
        "the owner nvim's swap file",
        timeout=15.0,
    )


def _nvim_follower_pane(world: E2EFollower) -> str:
    panes = world.tmux("list-panes", "-t", world.origin_pane, "-F", "#{pane_id}").stdout.split()
    [pane] = [p for p in panes if p != world.origin_pane]
    return pane


def _nvim_buffer(world: E2EFollower, sock: str, target: Path) -> int:
    nvim = world._nvim(sock)
    wanted = target.resolve()
    [number] = [
        b.number for b in nvim.api.list_bufs() if b.name and Path(b.name).resolve() == wanted
    ]
    return int(number)


def _second_nvim_swap_exists(world: E2EFollower, target: Path) -> int:
    """How many times a second nvim's SwapExists fires opening `target`."""
    out = world.workdir / f"second-{time.monotonic_ns()}.txt"
    script = (
        "let g:h = 0 | exe \"autocmd SwapExists * let g:h += 1 | let v:swapchoice = 'o'\""
        f" | edit {target} | call writefile([g:h], '{out}') | qa!"
    )
    subprocess.run(
        [
            "nvim",
            "--headless",
            "-u",
            "NONE",
            "-i",
            "NONE",
            "--cmd",
            f"set directory={_nvim_swap_dir(world)}//",
            "-c",
            script,
        ],
        env=world.env,
        timeout=20,
        check=False,
        capture_output=True,
    )
    return int(out.read_text().strip())


@pytest.mark.parametrize("held", [True, False], ids=["held", "free"])
@pytest.mark.parametrize("tool", ["Write", "Read"])
def test_an_adopted_nvim_keeps_swap_on_the_buffers_the_follower_makes(
    held: bool, tool: str, world: E2EFollower
) -> None:
    """Write: a fresh file retyped through show_fresh (its buffer is named
    under the opt-out). Read: a file loaded from disk (_open_from_disk)."""
    target = world.workdir / f"adopted_{tool.lower()}_{'held' if held else 'free'}.py"
    target.write_text("x = 1\n")
    world.start("nvim", "instant")
    sock = world.follower_target()
    _adopt(world)
    pane = _nvim_follower_pane(world)
    if held:
        _open_owner_nvim(world, target)

    if tool == "Write":
        _write(world, target, "x = 1\ny = 2\n")
    else:
        result = world.cli("hook", "post", stdin=payload("Read", target))
        assert result.stderr == ""

    nvim = world._nvim(sock)
    assert nvim.api.get_mode()["blocking"] is False
    buf = _nvim_buffer(world, sock, target)
    assert nvim.api.buf_get_lines(buf, 0, -1, True) == target.read_text().splitlines()
    assert nvim.funcs.swapname(buf) != ""
    _screen_is_clean(world, pane)
    _clean_log(world)
    if not held:
        # The follower's own swap is what warns a second editor now.
        assert _second_nvim_swap_exists(world, target) == 1


def test_an_adopted_nvim_re_reads_a_clean_swapped_buffer_with_no_prompt(
    world: E2EFollower,
) -> None:
    """A Read re-reads a clean open buffer (parity with tmux). `:edit!` runs
    the swap check again, and here another nvim holds the file's swap while
    the follower's buffer has its own: the re-read must not stop at
    ATTENTION, and the buffer keeps its swap afterwards."""
    target = world.workdir / "adopted_reread.py"
    target.write_text("x = 1\n")
    world.start("nvim", "instant")
    sock = world.follower_target()
    _adopt(world)
    pane = _nvim_follower_pane(world)
    _open_owner_nvim(world, target)
    assert world.cli("hook", "post", stdin=payload("Read", target)).stderr == ""
    nvim = world._nvim(sock)
    assert nvim.funcs.swapname(_nvim_buffer(world, sock, target)) != ""

    target.write_text("x = 2\n")  # a formatter, `sed -i`, a checkout
    assert world.cli("hook", "post", stdin=payload("Read", target)).stderr == ""

    assert nvim.api.get_mode()["blocking"] is False
    buf = _nvim_buffer(world, sock, target)
    assert nvim.api.buf_get_lines(buf, 0, -1, True) == ["x = 2"]
    assert nvim.funcs.swapname(buf) != ""
    _screen_is_clean(world, pane)
    _clean_log(world)


def test_a_dedicated_nvim_still_takes_no_swap(world: E2EFollower) -> None:
    target = world.workdir / "dedicated.py"
    world.start("nvim", "instant")
    sock = world.follower_target()
    _write(world, target, "x = 1\n")
    assert world._nvim(sock).funcs.swapname(_nvim_buffer(world, sock, target)) == ""
    assert _second_nvim_swap_exists(world, target) == 0


# ---------------------------------------------------------------- tmux Vim


def _narrow_follower(world: E2EFollower) -> str:
    pane = world.follower_target()
    width = world.resize_pane(pane, 49)
    assert width < 51, f"the follower pane is {width} columns: the ATTENTION block would not page"
    return pane


def _vim_eval(world: E2EFollower, pane: str, expr: str) -> str:
    out = world.workdir / f"vimeval-{time.monotonic_ns()}.txt"
    world.tmux("send-keys", "-t", pane, "Escape", "Escape")
    world.tmux("send-keys", "-t", pane, "-l", "--", f":call writefile(['E', {expr}], '{out}')")
    world.tmux("send-keys", "-t", pane, "Enter")
    world.wait_until(
        lambda: out.exists() and len(out.read_text().splitlines()) == 2, f"vim to evaluate {expr}"
    )
    return out.read_text().splitlines()[1]


def _second_vim_swap_exists(world: E2EFollower, target: Path) -> int:
    out = world.workdir / f"second-vim-{time.monotonic_ns()}.txt"
    pane = world.tmux(
        "new-window", "-d", "-t", world.session, "-P", "-F", "#{pane_id}", "vim -N -u NONE -i NONE"
    ).stdout.strip()
    time.sleep(0.5)
    for command in (
        ":let g:h = 0 | exe \"autocmd SwapExists * let g:h += 1 | let v:swapchoice = 'o'\"",
        f":edit {target}",
        f":call writefile(['E', g:h], '{out}')",
    ):
        world.tmux("send-keys", "-t", pane, "-l", "--", command)
        world.tmux("send-keys", "-t", pane, "Enter")
    world.wait_until(
        lambda: out.exists() and len(out.read_text().splitlines()) == 2, "the second Vim"
    )
    return int(out.read_text().splitlines()[1])


@pytest.mark.parametrize("held", [True, False], ids=["held", "free"])
def test_an_adopted_vim_keeps_swap_on_a_retyped_buffer_with_no_prompt(
    held: bool, world: E2EFollower
) -> None:
    target = world.workdir / f"adopted_vim_{'held' if held else 'free'}.py"
    target.write_text("x = 1\n")
    world.start("tmux", "instant")
    pane = _narrow_follower(world)
    _adopt(world)
    if held:
        world.tmux("new-window", "-d", "-t", world.session, f"vim -N {target}")
        swap = target.with_name(f".{target.name}.swp")
        world.wait_until(swap.exists, "the owner Vim's swap", timeout=15.0)

    _write(world, target, "x = 1\ny = 2\n")

    row, height = world.cursor_row(pane)
    assert row != height - 1, "cursor parked on the bottom row: a prompt is blocking"
    messages = world.vim_messages(pane)
    for needle in ("E325", "ATTENTION"):
        assert needle not in messages, f"{needle} reached the follower:\n{messages}"
    assert world.vim_buffer_bytes(pane) == target.read_bytes()
    assert _vim_eval(world, pane, "swapname('%')") != ""
    # the scoped `shortmess+=A` was restored
    assert "A" not in _vim_eval(world, pane, "&shortmess")
    _clean_log(world)
    if not held:
        assert _second_vim_swap_exists(world, target) == 1


def test_a_dedicated_vim_still_takes_no_swap(world: E2EFollower) -> None:
    target = world.workdir / "dedicated_vim.py"
    world.start("tmux", "instant")
    pane = _narrow_follower(world)
    _write(world, target, "x = 1\n")
    assert _vim_eval(world, pane, "swapname('%')") == ""
    assert _second_vim_swap_exists(world, target) == 0
