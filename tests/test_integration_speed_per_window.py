"""A speed change stays in the window it was made in.

N-C (2026-10-20): Alberto saw a speed change in one tmux window of a session
seem to change the other window's follower too. Two windows of ONE session
on a private server, a dedicated follower in each; the change goes through
the exact command line the `+` binding registers (run by tmux, which expands
`#{pane_id}` for the pane it runs against) and through the CLI.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import pytest
from e2e_harness import E2EFollower, e2e_world

from vim_ai_follower import config

pytestmark = pytest.mark.integration

START_SPEED = "lento"


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _speed(world: E2EFollower, window_id: str) -> str:
    state = json.loads((world.cache_dir / f"{window_id}.pane").read_text())
    speed = state["speed"]
    assert isinstance(speed, str)
    return speed


def _two_windows(world: E2EFollower) -> tuple[str, str, str]:
    """Start a follower in the world's window and in a second window of the
    same session; return the second window's pane and both window ids."""
    world.start("tmux", START_SPEED)
    second_pane = world.tmux(
        "new-window", "-t", world.session, "-P", "-F", "#{pane_id}", "sh"
    ).stdout.strip()
    second_window = world.tmux(
        "display-message", "-p", "-t", second_pane, "#{window_id}"
    ).stdout.strip()
    assert second_window != world.window_id
    world.cli(
        "start",
        "--backend",
        "tmux",
        "--speed",
        START_SPEED,
        environment=world.env_with(TMUX_PANE=second_pane),
    )
    assert _speed(world, world.window_id) == START_SPEED
    assert _speed(world, second_window) == START_SPEED
    return second_pane, world.window_id, second_window


def _registered_command(world: E2EFollower, key: str) -> str:
    """The run-shell command line the prefix `key` binding holds, as tmux
    lists it (the quoting is tmux's own)."""
    listing = world.tmux("list-keys", "-T", "prefix", key).stdout.strip()
    match = re.search(r"run-shell -b (.+)$", listing)
    assert match, f"no run-shell binding for prefix {key}: {listing!r}"
    command = match.group(1)
    if command[0] in "'\"" and command[-1] == command[0]:
        command = command[1:-1]
    assert "#{pane_id}" in command, command
    return command


def test_speed_up_through_the_binding_leaves_the_other_window_alone(
    world: E2EFollower,
) -> None:
    _second_pane, first_window, second_window = _two_windows(world)
    command = _registered_command(world, "+")
    world.tmux("run-shell", "-t", world.origin_pane, command)
    expected = config.next_speed(START_SPEED, "up")
    assert expected != START_SPEED
    world.wait_until(
        lambda: _speed(world, first_window) == expected,
        f"window {first_window}'s speed to become {expected}",
    )
    assert _speed(world, second_window) == START_SPEED


def test_speed_up_through_the_cli_leaves_the_other_window_alone(world: E2EFollower) -> None:
    second_pane, first_window, second_window = _two_windows(world)
    world.cli("speed-up", environment=world.env_with(TMUX_PANE=second_pane))
    assert _speed(world, second_window) == config.next_speed(START_SPEED, "up")
    assert _speed(world, first_window) == START_SPEED
