"""`claude-follow stop` clears the state first and bounds its teardown.

Two shapes the T16 review left open, against real nvims in a private tmux:

- an ADOPTED nvim sitting at a hit-enter prompt (the user's own `:ls`, a
  plugin's message): `stop` used to wait 10 s on the status float, 10 s more
  on `close_tab`, then exit 1 with a traceback and leave the `.pane` behind
  (M1). Now it sends nothing to an nvim that is waiting on its user, logs one
  line, clears the state and exits 0 at once;
- a DEDICATED follower nvim busy in `:!cmd`: nothing answers its socket, so
  the `qall!` never lands, and `stop` used to clear the state and leave the
  follower's pane in the user's window (M4). That pane is the follower's own,
  so `stop` closes it, and reaps the nvim process a busy nvim leaves behind
  when its pane goes (measured: the `--embed` server outlives SIGHUP and
  SIGTERM while `:!sleep` runs).

Real CLI, real tmux, real nvims, `HOME` isolated by the e2e harness; every CLI
run has a subprocess timeout so a regression FAILS instead of hanging."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from e2e_harness import CLI, E2EFollower, e2e_world

from vim_ai_follower.backends.nvim_connect import (
    CONNECT_TIMEOUT_SECONDS,
    RPC_TIMEOUT_SECONDS,
    probe_nvim,
)
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

_HANG_GUARD_SECONDS = 120.0
_STOP_BOUND_SECONDS = CONNECT_TIMEOUT_SECONDS + RPC_TIMEOUT_SECONDS + 2


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _timed_cli(world: E2EFollower, *args: str) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    result = subprocess.run(
        [str(CLI), *args],
        env=world.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_HANG_GUARD_SECONDS,
    )
    return result, time.monotonic() - started


def _processes_naming(text: str) -> str:
    """`pgrep -fl` lines whose command line carries `text` ('' for none)."""
    found = subprocess.run(["pgrep", "-fl", text], capture_output=True, text=True, check=False)
    return found.stdout.strip()


def _pane_ids(world: E2EFollower) -> list[str]:
    listing = world.tmux("list-panes", "-t", world.origin_pane, "-F", "#{pane_id}")
    return listing.stdout.split()


def test_stop_of_an_adopted_nvim_at_a_prompt_exits_at_once_and_clears_the_state(
    world: E2EFollower,
) -> None:
    socket_path = world.cache_dir / "nvim-u.sock"
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    tracked = world.workdir / "file.py"
    tracked.write_text("line 1\nline 2\n")
    # The user's own nvim, in the origin's window, adopted by the state below.
    created = world.tmux(
        "split-window",
        "-h",
        "-t",
        world.origin_pane,
        "-P",
        "-F",
        "#{pane_id}",
        "nvim",
        "-n",
        "-u",
        "NONE",
        "-i",
        "NONE",
        "--listen",
        str(socket_path),
        str(tracked),
    )
    user_pane = created.stdout.strip()
    world.wait_until(
        lambda: probe_nvim(str(socket_path)) == "answers", "the user's nvim", timeout=15.0
    )
    FollowerState.set(
        world.window_id,
        "nvim",
        str(socket_path),
        origin=world.origin_pane,
        server_pid=world.server_pid,
        adopted=True,
        shown_any=True,
        open_files=(str(tracked.resolve()),),
        base_dir=world.cache_dir,
    )
    # A multi-line echo raises a hit-enter prompt: blocking, until Enter.
    world.tmux("send-keys", "-t", user_pane, "-l", ':echo "one\\ntwo\\nthree"\r')
    world.wait_until(
        lambda: _blocking(world, socket_path), "the user's nvim at a prompt", timeout=10.0
    )

    stop, stop_seconds = _timed_cli(world, "stop")

    assert stop.returncode == 0, stop.stderr
    assert "Traceback" not in stop.stderr, stop.stderr
    assert stop_seconds < _STOP_BOUND_SECONDS, f"stop took {stop_seconds:.1f} s"
    assert not (world.cache_dir / f"{world.window_id}.pane").exists()
    lines = [
        line
        for line in (world.cache_dir / "hook.log").read_text().splitlines()
        if str(socket_path) in line
    ]
    assert len(lines) == 1, lines
    assert "waiting on its user" in lines[0]
    # The user's editor is theirs: still there, prompt and all.
    assert user_pane in _pane_ids(world)
    assert _blocking(world, socket_path)


def _blocking(world: E2EFollower, socket_path: Path) -> bool:
    from vim_ai_follower.backends.nvim_connect import last_probe_blocking

    return probe_nvim(str(socket_path)) == "answers" and last_probe_blocking(str(socket_path))


def test_stop_of_a_busy_dedicated_follower_leaves_no_pane_and_no_nvim(
    world: E2EFollower,
) -> None:
    panes_before = _pane_ids(world)
    world.start("nvim")
    socket_path = world.follower_target()
    follower_panes = [pane for pane in _pane_ids(world) if pane not in panes_before]
    assert len(follower_panes) == 1, follower_panes
    follower_pane = follower_panes[0]
    world.wait_until(lambda: probe_nvim(socket_path) == "answers", "the follower nvim")
    # `:!sleep` stands in for `:!make` typed into the follower: alive, silent.
    world.tmux("send-keys", "-t", follower_pane, "-l", ":!sleep 60\r")
    world.wait_until(
        lambda: probe_nvim(socket_path) == "silent", "the follower to be busy", timeout=10.0
    )

    stop, stop_seconds = _timed_cli(world, "stop")

    assert stop.returncode == 0, stop.stderr
    assert stop_seconds < _STOP_BOUND_SECONDS, f"stop took {stop_seconds:.1f} s"
    assert not (world.cache_dir / f"{world.window_id}.pane").exists()
    world.wait_until(
        lambda: follower_pane not in _pane_ids(world), "the follower's pane to close", timeout=5.0
    )
    assert world.origin_pane in _pane_ids(world)
    world.wait_until(
        lambda: _processes_naming(socket_path) == "", "the follower nvim to be gone", timeout=5.0
    )
