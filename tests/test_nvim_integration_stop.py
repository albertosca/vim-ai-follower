"""`claude-follow stop` clears the state first and bounds its teardown.

Two shapes the T16 review left open, against real nvims in a private tmux:

- an ADOPTED nvim sitting at a hit-enter prompt (the user's own `:ls`, a
  plugin's message): `stop` used to wait 10 s on the status float, 10 s more
  on `close_tab`, then exit 1 with a traceback and leave the `.pane` behind
  (M1). Now it answers the prompt first, as any acting step does (decision
  (a), the Vim follower's Escape pair), then tears down; an adopted nvim
  blocked in any other way (keys pending) gets nothing sent, one log line,
  and the state cleared, at once;
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


def _adopted_user_nvim(world: E2EFollower, socket_path: Path, tracked: Path) -> str:
    """The user's own nvim, in the origin's window, adopted by a state: its
    pane id, once it answers."""
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    tracked.write_text("line 1\nline 2\n")
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
    return user_pane


def _socket_lines(world: E2EFollower, socket_path: Path) -> list[str]:
    log_path = world.cache_dir / "hook.log"
    log = log_path.read_text() if log_path.exists() else ""
    return [line for line in log.splitlines() if str(socket_path) in line]


def test_stop_of_an_adopted_nvim_at_a_hit_enter_prompt_answers_it_then_tears_down(
    world: E2EFollower,
) -> None:
    socket_path = world.cache_dir / "nvim-u.sock"
    tracked = world.workdir / "file.py"
    user_pane = _adopted_user_nvim(world, socket_path, tracked)
    # A multi-line echo raises a hit-enter prompt: blocking, until answered.
    world.tmux("send-keys", "-t", user_pane, "-l", ':echo "one\\ntwo\\nthree"\r')
    world.wait_until(
        lambda: _blocking(world, socket_path), "the user's nvim at a prompt", timeout=10.0
    )

    stop, stop_seconds = _timed_cli(world, "stop")

    assert stop.returncode == 0, stop.stderr
    assert "Traceback" not in stop.stderr, stop.stderr
    assert stop_seconds < _STOP_BOUND_SECONDS, f"stop took {stop_seconds:.1f} s"
    assert not (world.cache_dir / f"{world.window_id}.pane").exists()
    log_path = world.cache_dir / "hook.log"
    log = log_path.read_text() if log_path.exists() else ""
    assert "dismissed a hit-enter prompt left before stop (sweep)" in log, log
    assert "waiting on its user" not in log, log
    # The user's editor is theirs: still there, prompt gone, file still open.
    assert user_pane in _pane_ids(world)
    assert probe_nvim(str(socket_path)) == "answers"
    assert not _blocking(world, socket_path)
    observer = world._nvim(str(socket_path))
    assert observer.call("bufloaded", str(tracked)) == 1


def test_stop_of_an_adopted_nvim_blocked_in_a_key_sequence_sends_nothing(
    world: E2EFollower,
) -> None:
    socket_path = world.cache_dir / "nvim-u.sock"
    tracked = world.workdir / "file.py"
    user_pane = _adopted_user_nvim(world, socket_path, tracked)
    # `q` waiting for its register: blocking in normal mode, nothing to answer.
    world.tmux("send-keys", "-t", user_pane, "q")
    world.wait_until(lambda: _blocking(world, socket_path), "the pending q", timeout=10.0)
    try:
        stop, stop_seconds = _timed_cli(world, "stop")

        assert stop.returncode == 0, stop.stderr
        assert "Traceback" not in stop.stderr, stop.stderr
        assert stop_seconds < _STOP_BOUND_SECONDS, f"stop took {stop_seconds:.1f} s"
        assert not (world.cache_dir / f"{world.window_id}.pane").exists()
        lines = _socket_lines(world, socket_path)
        assert len(lines) == 1, lines
        assert "waiting on its user" in lines[0]
        assert user_pane in _pane_ids(world)
        assert _blocking(world, socket_path)  # the pending key is still the user's
    finally:
        world.tmux("send-keys", "-t", user_pane, "Escape")


@pytest.mark.parametrize(
    ("keys", "mode", "named"),
    [
        ((":%s/line/LINE/gc", "Enter"), "r?", "a confirm prompt"),
        ((":let g:who = bufname('%')",), "c", "on the command line"),
    ],
    ids=["substitute-confirm", "half-typed-command"],
)
def test_stop_of_an_adopted_nvim_whose_user_is_mid_confirm_or_command_line_leaves_them_there(
    world: E2EFollower, keys: tuple[str, ...], mode: str, named: str
) -> None:
    # Neither is `blocking`, and the user's answer runs on whatever buffer is
    # current when it lands (task 22 review I1): the stop closes only the
    # follower's tabs the user is not in (T24c item 6), so the user stays in
    # their mode, and the state is cleared all the same.
    socket_path = world.cache_dir / "nvim-u.sock"
    tracked = world.workdir / "file.py"
    user_pane = _adopted_user_nvim(world, socket_path, tracked)
    world.tmux("send-keys", "-t", user_pane, *keys)
    world.wait_until(
        lambda: world._nvim(str(socket_path)).api.get_mode() == {"mode": mode, "blocking": False},
        f"the user's nvim in mode {mode}",
        timeout=10.0,
    )
    try:
        stop, stop_seconds = _timed_cli(world, "stop")

        assert stop.returncode == 0, stop.stderr
        assert "Traceback" not in stop.stderr, stop.stderr
        assert stop_seconds < _STOP_BOUND_SECONDS, f"stop took {stop_seconds:.1f} s"
        assert not (world.cache_dir / f"{world.window_id}.pane").exists()
        lines = _socket_lines(world, socket_path)
        assert len(lines) == 1, lines
        assert f"waiting on its user ({named}); closing only the follower's tabs" in lines[0]
        assert user_pane in _pane_ids(world)
        assert world._nvim(str(socket_path)).api.get_mode() == {"mode": mode, "blocking": False}
    finally:
        world.tmux("send-keys", "-t", user_pane, "Escape")
        world.tmux("send-keys", "-t", user_pane, "Escape")


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
