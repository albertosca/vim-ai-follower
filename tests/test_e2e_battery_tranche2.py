"""End-to-end tranche 2 of the machine-verifiable half of `qa/visual-battery.md`
(checks 11, 12, 17, 18, 19): the real `claude-follow` CLI, real hook payloads
on stdin, a real Vim/Neovim follower in an isolated world. Each test names
the battery check it replaces and the commit(s) it guards; what stays in the
battery is the part only a human can judge."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

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
