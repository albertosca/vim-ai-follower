"""A frozen nvim must never hang the CLI or a hook.

A follower's nvim that stops answering — SIGSTOPped (Ctrl-Z in its pane), or
wedged behind a request it never returns from — used to hang every command
that asked it anything: `status` and `stop` probe each window's follower for
liveness (`FollowerState.get` → `is_alive`, an unbounded `pynvim.attach` +
`get_mode`), and a hook did the same before it could decide there was nothing
to animate. Claude Code then waited out its 600 s hook timeout on every tool
call. Liveness is now a bounded raw `nvim_get_mode` probe, and every connect
and request carries a deadline.

Real CLI, real tmux, real nvims, `HOME` isolated by the e2e harness. Each CLI
run has a 60 s subprocess timeout, so a regression FAILS instead of freezing
the suite; the frozen nvim is resumed and killed in a `finally`."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from e2e_harness import CLI, E2EFollower, e2e_world, payload

from vim_ai_follower.backends.nvim_connect import CONNECT_TIMEOUT_SECONDS, RPC_TIMEOUT_SECONDS
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

_HANG_GUARD_SECONDS = 60.0


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _new_window(world: E2EFollower) -> tuple[str, str]:
    """A second tmux window in the world's private server: (window id, pane id)."""
    created = world.tmux("new-window", "-d", "-P", "-F", "#{window_id} #{pane_id}")
    window_id, pane_id = created.stdout.split()
    return window_id, pane_id


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


@contextlib.contextmanager
def _frozen_nvim(world: E2EFollower, socket_path: Path) -> Iterator[int]:
    """A headless nvim listening on `socket_path`, then SIGSTOPped: the socket
    accepts connections (the kernel does that) and nothing ever answers."""
    process = subprocess.Popen(
        ["nvim", "--headless", "-n", "-u", "NONE", "--listen", str(socket_path)],
        env=world.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        world.wait_until(socket_path.exists, f"{socket_path} to be bound", timeout=10.0)
        os.kill(process.pid, signal.SIGSTOP)
        yield process.pid
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(process.pid, signal.SIGCONT)
        process.kill()
        process.wait(timeout=10)


def test_status_and_stop_return_promptly_with_a_frozen_nvim(world: E2EFollower) -> None:
    world.start("nvim")
    live_socket = world.follower_target()
    frozen_window, frozen_pane = _new_window(world)
    _observer_window, observer_pane = _new_window(world)
    frozen_socket = world.cache_dir / f"nvim-{frozen_window}.sock"
    target_file = world.workdir / "frozen.py"
    target_file.write_text("print('frozen')\n")

    with _frozen_nvim(world, frozen_socket) as frozen_pid:
        FollowerState.set(
            frozen_window,
            "nvim",
            str(frozen_socket),
            origin=frozen_pane,
            adopted=False,
            server_pid=world.server_pid,
            base_dir=world.cache_dir,
        )

        status, status_seconds = _timed_cli(world, "status", pane_id=observer_pane)
        assert status.returncode == 0, status.stderr
        assert status_seconds < 5.0, f"status took {status_seconds:.1f} s"
        assert f"{world.window_id}: nvim {live_socket}" in status.stdout, status.stdout
        # Listed, marked: a silent nvim is busy or stopped, never "gone".
        frozen_line = f"{frozen_window}: nvim {frozen_socket}"
        assert frozen_line in status.stdout, status.stdout
        assert "not answering" in status.stdout.split(frozen_line, 1)[1].splitlines()[0]
        assert frozen_socket.exists(), "the sweep removed a socket a .pane still names"

        hook, hook_seconds = _timed_cli(
            world, "hook", "post", pane_id=frozen_pane, stdin=payload("Write", target_file)
        )
        assert hook.returncode == 0, hook.stderr
        assert hook_seconds < 15.0, f"the hook took {hook_seconds:.1f} s"
        log = (world.cache_dir / "hook.log").read_text()
        timeouts = [line for line in log.splitlines() if "did not answer within" in line]
        assert len(timeouts) == 1, log
        assert str(frozen_socket) in timeouts[0]

        stop, stop_seconds = _timed_cli(world, "stop", pane_id=frozen_pane)
        assert stop.returncode == 0, stop.stderr
        # The probe, then at most the connect and RPC deadlines for every
        # step together (commands._tear_down_nvim's budget), plus startup.
        stop_bound = CONNECT_TIMEOUT_SECONDS + RPC_TIMEOUT_SECONDS + 2
        assert stop_seconds < stop_bound, f"stop took {stop_seconds:.1f} s"
        assert not (world.cache_dir / f"{frozen_window}.pane").exists()
        # The live follower in the other window is untouched by any of it.
        assert (world.cache_dir / f"{world.window_id}.pane").exists()
        os.kill(frozen_pid, 0)  # still there: nothing here kills a frozen nvim
