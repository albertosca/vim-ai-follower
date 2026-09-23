"""End-to-end tranche 2 of the machine-verifiable half of `qa/visual-battery.md`
(checks 11, 12, 17, 18, 19): the real `claude-follow` CLI, real hook payloads
on stdin, a real Vim/Neovim follower in an isolated world. Each test names
the battery check it replaces and the commit(s) it guards; what stays in the
battery is the part only a human can judge."""

from __future__ import annotations

import os
import signal
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from e2e_harness import E2EFollower, e2e_world, payload

pytestmark = pytest.mark.integration


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _write_through_hooks(world: E2EFollower, path: Path, content: str) -> None:
    """One synchronous Write: pre snapshots the absent file, post animates it."""
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text(content)
    world.cli("hook", "post", stdin=payload("Write", path))


def test_three_writes_land_as_three_real_tabs(world: E2EFollower) -> None:
    """Battery check 11 — guards 7abf023 / 5253e00 / 142b76d (nvim backend).

    Serial on purpose: parallel hooks are serialized by the animation-slot
    claim and only one would animate. Tabs and contents are read over RPC —
    the tab line on screen is a scrolled, abbreviated rendering."""
    world.start("nvim", "instant")
    sock = world.follower_target()
    files = {name: world.workdir / f"multi_{name}.py" for name in ("alpha", "beta", "gamma")}
    for name, path in files.items():
        _write_through_hooks(world, path, f"# TAB {name.upper()}\nvalue = '{name}'\n")

    nvim = world._nvim(sock)
    tabs = nvim.api.list_tabpages()
    # The PASS table's other rows: exactly three tabs (no leftover blank one)
    # and one window each (not splits stacked inside a tab).
    assert len(tabs) == 3, f"expected 3 tabs, found {len(tabs)}"
    assert [len(nvim.api.tabpage_list_wins(tab)) for tab in tabs] == [1, 1, 1]
    shown = [
        Path(nvim.api.buf_get_name(nvim.api.win_get_buf(nvim.api.tabpage_get_win(tab)))).resolve()
        for tab in nvim.api.list_tabpages()
    ]
    for name, path in files.items():
        assert shown.count(path.resolve()) == 1, f"{name} is not in exactly one tab: {shown}"
        assert world.nvim_buffer_lines(sock, path) == [f"# TAB {name.upper()}", f"value = '{name}'"]


def _status_float_body(nvim: Any) -> list[str] | None:
    """The follower's floating status window's lines, or None when no
    floating window is open. Relative-positioned windows are floats."""
    api = nvim.api
    for win in api.list_wins():
        if api.win_get_config(win).get("relative", ""):
            return [line.strip() for line in api.buf_get_lines(api.win_get_buf(win), 0, -1, True)]
    return None


def _typing_line_marks(nvim: Any, path: Path) -> list[int]:
    """Rows of `path`'s buffer carrying a VafTypingLine line highlight."""
    wanted = path.resolve()
    ns = nvim.api.create_namespace("vaf")
    for buf in nvim.api.list_bufs():
        name = nvim.api.buf_get_name(buf)
        if name and Path(name).resolve() == wanted:
            marks = nvim.api.buf_get_extmarks(buf, ns, 0, -1, {"details": True})
            return [row for _id, row, _col, d in marks if d.get("line_hl_group") == "VafTypingLine"]
    return []


def test_first_animation_shows_writing_and_keeps_the_typing_highlight(
    world: E2EFollower,
) -> None:
    """Battery check 12 — guards 28937f0 (the ordinary path set no
    "Writing..." body) and 0998397 + c42b77a (`:colorscheme` runs `hi clear`,
    which wiped VafTypingLine when it ran AFTER the highlight was defined).

    The isolated HOME has no gruvbox, and `silent! colorscheme gruvbox` on a
    missing scheme never reaches `hi clear` — so a stand-in gruvbox that does
    `hi clear` is installed first, and its having run is asserted, or the
    ordering half of this test would pass vacuously. Whether the real colours
    READ well stays in the battery."""
    colors = world.home / ".config" / "nvim" / "colors"
    colors.mkdir(parents=True)
    (colors / "gruvbox.vim").write_text("hi clear\nlet g:colors_name = 'gruvbox'\n")

    world.start("nvim", "lento")  # a fresh nvim: the scheme is forced once per process
    sock = world.follower_target()
    path = world.workdir / "writing_cue.py"
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text("".join(f"line_{i} = {i}\n" for i in range(12)))
    proc = world.cli_background("hook", "post", stdin=payload("Write", path))
    world.wait_for_animating("running")
    world.wait_until(
        lambda: "line_1" in "".join(world.nvim_buffer_lines(sock, path)),
        "the animation to be typing",
        timeout=60.0,
    )

    nvim = world._nvim(sock)
    assert nvim.eval("get(g:, 'colors_name', '')") == "gruvbox", "stand-in scheme never ran"
    assert nvim.api.get_hl(0, {"name": "VafTypingLine"}), "VafTypingLine was wiped by hi clear"
    # The group surviving is half the promise; the other half is that it is
    # APPLIED — a line_hl_group extmark on the line being typed.
    world.wait_until(
        lambda: _typing_line_marks(nvim, path) != [],
        "a VafTypingLine extmark on the line being typed",
        timeout=30.0,
    )
    body = _status_float_body(nvim)
    assert body is not None and "Writing..." in body, f"float body mid-animation: {body!r}"

    world.wait_for_hook_exit(proc)
    assert _status_float_body(nvim) is None, "the float outlived the animation"

    # "On EVERY animation": the second one must show the cue too. The float's
    # scratch buffer is hidden, not wiped, when its window closes; a clear()
    # that failed to wipe it made every later buf_set_name hit E95, swallowed,
    # so the cue silently never came back (measured 2026-09-23).
    second = world.workdir / "writing_cue_2.py"
    world.cli("hook", "pre", stdin=payload("Write", second))
    second.write_text("".join(f"again_{i} = {i}\n" for i in range(12)))
    proc = world.cli_background("hook", "post", stdin=payload("Write", second))
    world.wait_for_animating("running")
    world.wait_until(
        lambda: "again_1" in "".join(world.nvim_buffer_lines(sock, second)),
        "the second animation to be typing",
        timeout=60.0,
    )
    body = _status_float_body(nvim)
    assert body is not None and "Writing..." in body, f"second animation's float: {body!r}"
    world.wait_for_hook_exit(proc)
    assert _status_float_body(nvim) is None, "the second animation's float outlived it"


# --------------------------------------------------------------- Check 17


def _open_owner_vim(world: E2EFollower, target: Path) -> str:
    """A second real Vim holding `target`, in its own (detached) window of the
    world's own server, so the follower window's geometry is untouched.
    Returns its pane id once the swap exists — without one there is nothing
    for the follower to answer and the test would measure nothing."""
    pane = world.tmux(
        "new-window", "-d", "-t", world.session, "-P", "-F", "#{pane_id}", f"vim -N {target}"
    ).stdout.strip()
    swap = target.with_name(f".{target.name}.swp")
    world.wait_until(swap.exists, f"the owner Vim's swap {swap}", timeout=15.0)
    return pane


def _navigate_and_assert_clean(world: E2EFollower, pane: str, target: Path) -> None:
    world.cli("hook", "post", stdin=payload("Read", target))
    row, height = world.cursor_row(pane)
    assert row != height - 1, "cursor parked on the bottom row: a prompt is blocking"
    messages = world.vim_messages(pane)
    for needle in ("E325", "ATTENTION", "already exists"):
        assert needle not in messages, f"{needle} reached the follower:\n{messages}"
    assert world.vim_buffer_bytes(pane) == target.read_bytes()


def _narrow_follower(world: E2EFollower) -> str:
    pane = world.follower_target()
    width = world.resize_pane(pane, 49)
    assert width < 51, (
        f"the follower pane is {width} columns; at 51 or more the ATTENTION block "
        "does not page and a clean pane proves nothing"
    )
    return pane


def test_a_live_owners_swap_is_edited_anyway_and_the_owner_still_writes(
    world: E2EFollower,
) -> None:
    """Battery check 17 (live) — guards e0bb5be, tmux backend, at 49 columns:
    the ATTENTION block used to stall `:tab drop` twice over (pager, then the
    question), so every later keystroke answered a prompt."""
    target = world.workdir / "swap_live.py"
    target.write_text("owner = 'LIVE'\n")
    world.start("tmux", "instant")
    pane = _narrow_follower(world)
    owner = _open_owner_vim(world, target)
    disk_before = target.read_bytes()

    _navigate_and_assert_clean(world, pane, target)

    # The PASS row "its content was touched" is a FAIL. The owner is another
    # process: the only channel the follower has into its content is the FILE
    # (a write the owner would then reload), so that is what must not move.
    assert target.read_bytes() == disk_before
    world.tmux("send-keys", "-t", owner, "Go# owner edit", "Escape", ":w", "Enter")
    world.wait_until(lambda: "# owner edit" in target.read_text(), "the owner Vim's :w")


def test_a_stale_swap_is_edited_anyway_and_left_on_disk(world: E2EFollower) -> None:
    """Battery check 17 (stale) — guards e0bb5be: (E)dit anyway, never
    (D)elete, so a crash's recovery data survives. SIGKILL, never SIGTERM:
    on SIGTERM Vim removes its own swap and nothing stale is left."""
    target = world.workdir / "swap_stale.py"
    target.write_text("owner = 'STALE'\n")
    world.start("tmux", "instant")
    pane = _narrow_follower(world)
    owner = _open_owner_vim(world, target)
    world.tmux("send-keys", "-t", owner, "oWORK LOST IN THE CRASH", "Escape")
    pid = int(world.tmux("display-message", "-p", "-t", owner, "#{pane_pid}").stdout.strip())
    os.kill(pid, signal.SIGKILL)
    swap = target.with_name(f".{target.name}.swp")
    assert swap.exists(), "the kill removed the swap: nothing stale to answer"

    _navigate_and_assert_clean(world, pane, target)
    assert swap.exists(), "the follower deleted the stale swap"


# --------------------------------------------------------------- Check 18


def _open_owner_nvim(world: E2EFollower, target: Path) -> str:
    """A second real nvim holding `target`'s swap, in a detached window.
    Returns its pane id once the swap exists in the isolated HOME's
    stdpath('state')/swap — without it the check would be vacuous."""
    owner = world.tmux(
        "new-window",
        "-d",
        "-t",
        world.session,
        "-P",
        "-F",
        "#{pane_id}",
        f"nvim -u NONE -i NONE {target}",
    ).stdout.strip()
    swap_dir = world.home / ".local" / "state" / "nvim" / "swap"
    world.wait_until(
        lambda: swap_dir.exists() and any(target.name in p.name for p in swap_dir.iterdir()),
        "the owner nvim's swap file",
        timeout=15.0,
    )
    return owner


def _assert_clean_log_and_usable_owner(world: E2EFollower, owner: str) -> None:
    """The battery's log grep (`-i` error/traceback/E325) and "the other nvim
    is still USABLE" — proved by making it run a command, not by its pane
    merely existing."""
    log_path = world.cache_dir / "hook.log"
    # Every hook configures logging (creating the file) before doing anything,
    # so a missing log means the grep below would search nothing.
    assert log_path.exists(), "no hook.log: the hooks never ran, or logged elsewhere"
    log = log_path.read_text()
    for needle in ("traceback", "error", "e325"):
        assert needle not in log.lower(), f"{needle!r} in hook.log:\n{log}"
    probe = world.workdir / f"owner_alive_{owner.lstrip('%')}.txt"
    command = f":call writefile(['alive'], '{probe}')"
    world.tmux("send-keys", "-t", owner, "Escape", command, "Enter")
    world.wait_until(probe.exists, "the owner nvim to run a command", timeout=10.0)


def test_a_swap_held_file_opens_in_nvim_and_the_hook_survives(world: E2EFollower) -> None:
    """Battery check 18 — guards d817f08. `bufload` raised E325 straight out
    of the RPC call and killed the HOOK PROCESS instead of showing the file.
    The owner is an NVIM, not a Vim: nvim's swap dir is
    stdpath('state')/swap, which is where the follower nvim looks; a Vim-made
    swap beside the file would make this vacuous. `world.cli` asserts the
    hook's exit code, which is the whole verdict here — there is no dialog to
    see, only a dead hook."""
    target = world.workdir / "nvim_swap.py"
    target.write_text("held = 'BY OWNER'\n")
    world.start("nvim", "instant")
    sock = world.follower_target()
    owner = _open_owner_nvim(world, target)

    result = world.cli("hook", "post", stdin=payload("Read", target))
    assert result.stderr == ""
    assert world.nvim_buffer_lines(sock, target) == ["held = 'BY OWNER'"]
    _assert_clean_log_and_usable_owner(world, owner)


def test_a_swap_held_file_whose_follower_buffer_vanished_opens_on_edit(
    world: E2EFollower,
) -> None:
    """Battery check 18, the OTHER caller named in its purpose: `apply_edit`'s
    vanished-buffer branch loads from disk through the same `_open_from_disk`,
    so a swap-held file crashed the Edit hook the same way. The follower must
    already track the file (so the edit goes to apply_edit, not a fresh
    retype) and its buffer must be gone (wiped, as a user `:bwipeout` does)."""
    target = world.workdir / "nvim_swap_edit.py"
    world.start("nvim", "instant")
    sock = world.follower_target()
    world.cli("hook", "pre", stdin=payload("Write", target))
    target.write_text("before = 1\n")
    world.cli("hook", "post", stdin=payload("Write", target))
    nvim = world._nvim(sock)
    wanted = target.resolve()
    [number] = [
        b.number for b in nvim.api.list_bufs() if b.name and Path(b.name).resolve() == wanted
    ]
    nvim.command(f"bwipeout! {number}")
    owner = _open_owner_nvim(world, target)

    world.cli("hook", "pre", stdin=payload("Edit", target))
    target.write_text("before = 1\nafter = 2\n")
    result = world.cli("hook", "post", stdin=payload("Edit", target))
    assert result.stderr == ""
    assert world.nvim_buffer_lines(sock, target) == ["before = 1", "after = 2"]
    _assert_clean_log_and_usable_owner(world, owner)


# --------------------------------------------------------------- Check 19


def test_read_navigation_shows_disk_and_clamps_the_offset(world: E2EFollower) -> None:
    """Battery check 19 — guards 98b6201. A Read of a never-animated file
    opened an EMPTY buffer (ensure_showing delegated to goto_file, which never
    reads disk), and an offset past EOF then raised "Invalid cursor line" out
    of the hook. The adopted-nvim half stays manual (see the battery); this
    nvim is launched, so the buffer must end nomodifiable."""
    target = world.workdir / "read_nav.py"
    lines = [f"row_{i} = {i}" for i in range(1, 47)]
    target.write_text("".join(line + "\n" for line in lines))
    world.start("nvim", "instant")
    sock = world.follower_target()
    nvim = world._nvim(sock)

    def current_is_target() -> bool:
        return Path(nvim.api.buf_get_name(nvim.api.get_current_buf())).resolve() == target.resolve()

    result = world.cli("hook", "post", stdin=payload("Read", target, offset=5))
    assert result.stderr == ""
    assert world.nvim_buffer_lines(sock, target) == lines
    # cursor and &modifiable are read from the CURRENT window/buffer: prove
    # that is the target before trusting either.
    assert current_is_target()
    assert nvim.api.win_get_cursor(0)[0] == 5

    result = world.cli("hook", "post", stdin=payload("Read", target, offset=9999))
    assert result.stderr == ""
    assert current_is_target()
    assert nvim.api.win_get_cursor(0)[0] == len(lines)
    assert nvim.eval("&modifiable") == 0
