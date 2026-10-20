"""The guide's promise for an ADOPTED tmux Vim, through the real CLI: the
first file Claude writes opens in its own tab, and the user's tab keeps the
buffer they were editing, unsaved line and all (T8 matrix m5: only the
nvim twin and the unit test of `shown_any` had pinned it).

`start` adopts the one Vim in the origin's window and records
`shown_any=True`, which is what makes the first show_fresh open a tab
instead of typing into the current window."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from e2e_harness import E2EFollower, e2e_world, payload

pytestmark = pytest.mark.integration

UNSAVED = "USER UNSAVED"


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _vim_tabs(world: E2EFollower, pane: str) -> list[str]:
    """One line per tab, `<name of its first window's buffer>`, then the
    user's buffer's lines, straight out of Vim."""
    out = world.workdir / "tabs.txt"
    out.unlink(missing_ok=True)
    world.tmux("send-keys", "-t", pane, "Escape", "Escape")
    world.tmux(
        "send-keys",
        "-t",
        pane,
        "-l",
        "--",
        ":call writefile(map(range(1, tabpagenr('$')),"
        " {_, tab -> 'TAB ' . fnamemodify(bufname(tabpagebuflist(tab)[0]), ':t')})"
        " + getbufline(bufnr('user.txt'), 1, '$') + ['END'], '" + str(out) + "')\r",
    )
    world.wait_until(
        lambda: out.exists() and out.read_text().endswith("END\n"), "vim's tab dump", timeout=10.0
    )
    return out.read_text().splitlines()[:-1]


def test_the_first_file_in_an_adopted_tmux_vim_opens_in_its_own_tab(
    world: E2EFollower,
) -> None:
    user = world.workdir / "user.txt"
    user.write_text("user line\n")
    config = world.home / ".config" / "claude-vim-follower" / "config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"adopt_existing": True}))
    pane = world.tmux(
        "split-window", "-d", "-h", "-t", world.origin_pane, "-c", str(world.workdir),
        "-P", "-F", "#{pane_id}", f"vim -N -u NONE -i NONE {user}",
    ).stdout.strip()  # fmt: skip
    world.wait_until(
        lambda: "user line" in world.capture_pane(pane), "the user's Vim showing user.txt"
    )
    world.tmux("send-keys", "-t", pane, "-l", "--", "Go" + UNSAVED)
    world.tmux("send-keys", "-t", pane, "Escape")

    world.start("tmux", "instant")
    state = json.loads((world.cache_dir / f"{world.window_id}.pane").read_text())
    assert (state["target"], state["adopted"], state["shown_any"]) == (pane, True, True)

    fresh = world.workdir / "fresh.py"
    world.cli("hook", "pre", stdin=payload("Write", fresh))
    fresh.write_text("fresh = 1\n")
    post = world.cli("hook", "post", stdin=payload("Write", fresh))
    assert post.returncode == 0, post.stderr

    log = (world.cache_dir / "hook.log").read_text()
    assert "Traceback" not in log, log
    assert _vim_tabs(world, pane) == ["TAB user.txt", "TAB fresh.py", "user line", UNSAVED]
