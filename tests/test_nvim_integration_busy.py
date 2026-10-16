"""A BUSY nvim is alive: silence is not death.

An nvim running `:!make` (here `:!sleep`), a `system()` call or a long Lua
loop accepts connections and answers nothing until it is done — exactly like
a SIGSTOPped one. Only "absent" (no socket file, nobody listening) may make a
follower read as gone: a `stop` in ANOTHER window used to garbage-collect a
busy follower's state, pending remainder and snapshots (review I1).

Real CLI, real tmux, real nvims in tmux panes, `HOME` isolated by the e2e
harness; every CLI run has a subprocess timeout so a regression FAILS."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from e2e_harness import CLI, E2EFollower, e2e_world

from vim_ai_follower import control, snapshot
from vim_ai_follower.backends.nvim_connect import probe_nvim
from vim_ai_follower.diff import compute_edit_script
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

_HANG_GUARD_SECONDS = 120.0


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _timed_cli(
    world: E2EFollower, *args: str, pane_id: str, stdin: str | None = None
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    result = subprocess.run(
        [str(CLI), *args],
        input=stdin,
        env=world.env_with(TMUX_PANE=pane_id),
        capture_output=True,
        text=True,
        check=False,
        timeout=_HANG_GUARD_SECONDS,
    )
    return result, time.monotonic() - started


def _nvim_window(world: E2EFollower, socket_path: Path, *files: Path) -> tuple[str, str]:
    """A new tmux window running a real (TUI) nvim listening on socket_path:
    (window id, pane id), once it answers."""
    created = world.tmux(
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{window_id} #{pane_id}",
        "nvim",
        "-n",
        "-u",
        "NONE",
        "-i",
        "NONE",
        "--listen",
        str(socket_path),
        *(str(file) for file in files),
    )
    window_id, pane_id = created.stdout.split()
    world.wait_until(
        lambda: probe_nvim(str(socket_path)) == "answers", f"nvim on {socket_path}", timeout=15.0
    )
    return window_id, pane_id


def test_a_stop_elsewhere_keeps_a_busy_followers_state(world: E2EFollower) -> None:
    busy_socket = world.cache_dir / "nvim-busy.sock"
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    tracked = world.workdir / "busy.py"
    tracked.write_text("a\n")
    busy_window, busy_pane = _nvim_window(world, busy_socket, tracked)
    FollowerState.set(
        busy_window,
        "nvim",
        str(busy_socket),
        origin=busy_pane,
        adopted=True,
        open_files=(str(tracked),),
        base_dir=world.cache_dir,
    )
    control.save_pending_apply_edit(
        busy_window,
        compute_edit_script("a\n", "a\nb\n"),
        0.0,
        base_dir=world.cache_dir,
        file_path=str(tracked),
        partial="a\n",
    )
    snapshot.save(busy_window, str(tracked), "a\n", base_dir=world.cache_dir / "snapshots")
    pane_file = world.cache_dir / f"{busy_window}.pane"
    pending_file = world.cache_dir / f"{busy_window}.pending_animation.json"
    snapshot_dir = world.cache_dir / "snapshots" / busy_window
    assert pane_file.exists() and pending_file.exists() and any(snapshot_dir.iterdir())

    # `:!sleep` stands in for `:!make`: the nvim is alive and answers nothing.
    world.tmux("send-keys", "-t", busy_pane, "-l", ":!sleep 20\r")
    world.wait_until(
        lambda: probe_nvim(str(busy_socket)) == "silent", "the nvim to be busy", timeout=10.0
    )

    stop, stop_seconds = _timed_cli(world, "stop", pane_id=world.origin_pane)
    assert stop.returncode == 0, stop.stderr
    assert stop_seconds < 10.0, f"stop took {stop_seconds:.1f} s"
    assert probe_nvim(str(busy_socket)) == "silent", "the nvim finished its command too early"
    assert pane_file.exists(), "a stop elsewhere collected a busy follower's state"
    assert pending_file.exists(), "a stop elsewhere dropped a busy follower's remainder"
    assert any(snapshot_dir.iterdir()), "a stop elsewhere dropped a busy follower's snapshots"
    assert busy_socket.exists()

    status, status_seconds = _timed_cli(world, "status", pane_id=world.origin_pane)
    assert status.returncode == 0, status.stderr
    assert status_seconds < 5.0, f"status took {status_seconds:.1f} s"
    assert f"{busy_window}: nvim {busy_socket}" in status.stdout, status.stdout
    assert "not answering" in status.stdout, status.stdout
