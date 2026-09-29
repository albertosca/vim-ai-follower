"""End-to-end tranche 3 of the machine-verifiable half of `qa/visual-battery.md`
(checks 1-10): the real `claude-follow` CLI, real hook payloads on stdin, a
real Vim/Neovim follower in an isolated world. Each test names the battery
check it replaces and the commit(s) it guards; what stays in the battery is
the part only a human can judge."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from e2e_harness import E2EFollower, e2e_world

from vim_ai_follower.commands import VIM_NEEDS_TMUX

pytestmark = pytest.mark.integration


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _write_config(world: E2EFollower, settings: dict[str, str]) -> None:
    path = world.home / ".config" / "claude-vim-follower" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings))


def _assert_private_server(world: E2EFollower) -> None:
    """The tmux every child of this world reaches is the PRIVATE server. The
    checks without TMUX_PANE depend on it twice: session resolution then
    scans `tmux list-panes -a` for an ancestor pane, and on the developer's
    real server that scan could resolve pytest to his live window."""
    socket_path = world.tmux("display-message", "-p", "#{socket_path}").stdout.strip()
    assert Path(socket_path).resolve().is_relative_to(Path(world.tmux_tmpdir).resolve()), (
        f"tmux answered from {socket_path}, not from the private {world.tmux_tmpdir}"
    )


def _all_panes(world: E2EFollower) -> list[str]:
    return world.tmux("list-panes", "-a", "-F", "#{pane_id}").stdout.split()


def _follower_leftovers(world: E2EFollower) -> list[str]:
    cache = world.cache_dir
    if not cache.exists():
        return []
    return sorted(p.name for glob in ("*.pane", "nvim-*.sock") for p in cache.glob(glob))


# --------------------------------------------------------------- Check 10


def test_tmux_backend_outside_tmux_fails_loudly_and_opens_nothing(world: E2EFollower) -> None:
    """Battery check 10 — guards fe87f5b. `start` with `backend: tmux` from a
    terminal outside tmux: the exact actionable message on STDERR (an error,
    like its sibling "could not open a standalone nvim window"), nothing on
    stdout (so no traceback anywhere), exit 1, and nothing opened — no pane,
    no follower state, no nvim socket.

    Driven through the CONFIG FILE, as the battery does, not a --backend flag.
    Outside tmux means no TMUX_PANE; TMUX_TMPDIR stays private, because the
    pid-ancestry scan that runs without TMUX_PANE asks tmux for every pane."""
    _write_config(world, {"backend": "tmux"})
    _assert_private_server(world)
    outside = world.env_with(drop=("TMUX_PANE",), TERM_SESSION_ID="e2e")
    panes_before = _all_panes(world)

    result = world.cli("start", expect_rc=1, env=outside)

    assert result.stderr == VIM_NEEDS_TMUX + "\n"
    assert result.stdout == ""
    assert _all_panes(world) == panes_before, "a pane opened"
    assert _follower_leftovers(world) == [], "start left follower state behind"
