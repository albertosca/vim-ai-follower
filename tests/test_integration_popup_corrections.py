"""A late "Nothing to pause" correction never covers the next press's own
popup (sweep-5 T22, final m2), on a real tmux server with a client attached.

tmux shows one popup per client and drops a second one silently, exit 0, so
a correction a hook's exit drain scheduled (1.6 s after the drain, for 1.5 s)
swallowed the "Paused" of a P pressed for the NEXT hook. The press now
withdraws it first: a correction not open yet never opens, one open closes
and is waited for. A popup is seen by its shell, a child of the tmux server
running the popup's own command (a dropped popup's command never runs)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator

import pytest
from e2e_harness import E2EFollower, attached_client, e2e_world

pytestmark = pytest.mark.integration


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _open_popups(world: E2EFollower) -> str:
    """The commands of the popups open now: the tmux server's children."""
    server = world.tmux("display-message", "-p", "#{pid}").stdout.strip()
    listing = subprocess.run(
        ["ps", "-A", "-ww", "-o", "ppid=,command="], capture_output=True, text=True, check=True
    ).stdout
    return "\n".join(
        line.split(None, 1)[1]
        for line in listing.splitlines()
        if line.split(None, 1)[0] == server and len(line.split(None, 1)) == 2
    )


def _schedule_correction(world: E2EFollower, target: str, after_seconds: float) -> None:
    script = (
        "from vim_ai_follower import control, tmux; "
        f"tmux.show_popup({target!r}, 'Nothing to pause', after_seconds={after_seconds}, "
        f"withdraw_token=control.new_correction_token({world.window_id!r}))"
    )
    subprocess.run([sys.executable, "-P", "-c", script], env=world.env, check=True)


def _press_pause_for_a_running_hook(world: E2EFollower) -> None:
    # A live "running" owner (this test process) for the press to address.
    world.animating_path.write_text(f"{os.getpid()} running")
    world.cli("pause")


@pytest.mark.parametrize("when", ["open", "not-open-yet"])
def test_a_press_withdraws_a_late_correction_and_its_own_popup_shows(
    world: E2EFollower, when: str
) -> None:
    world.start("tmux", "instant")
    target = json.loads((world.cache_dir / f"{world.window_id}.pane").read_text())["target"]
    with attached_client(world):
        if when == "open":
            _schedule_correction(world, target, after_seconds=0.1)
            world.wait_until(
                lambda: "echo 'Nothing to pause'" in _open_popups(world),
                "the correction popup open",
            )
        else:
            _schedule_correction(world, target, after_seconds=1.0)
        _press_pause_for_a_running_hook(world)
        world.wait_until(
            lambda: "echo Paused" in _open_popups(world),
            "the press's own Paused popup",
            timeout=3.0,
        )
        assert "Nothing to pause" not in _open_popups(world)
        world.animating_path.unlink()
