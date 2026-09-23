"""End-to-end tranche 2 of the machine-verifiable half of `qa/visual-battery.md`
(checks 11, 12, 17, 18, 19): the real `claude-follow` CLI, real hook payloads
on stdin, a real Vim/Neovim follower in an isolated world. Each test names
the battery check it replaces and the commit(s) it guards; what stays in the
battery is the part only a human can judge."""

from __future__ import annotations

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
    body = _status_float_body(nvim)
    assert body is not None and "Writing..." in body, f"float body mid-animation: {body!r}"

    world.wait_for_hook_exit(proc)
    assert _status_float_body(nvim) is None, "the float outlived the animation"
