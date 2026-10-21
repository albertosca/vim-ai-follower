"""Two tmux clients of one session tie on #{client_activity} (1 s
resolution): the take-over question goes to the one whose terminal has focus
(T24a re-review 2 m13). It used to go to the first one listed, where the
user's `y` never arrived and the question timed out.

Real CLI, real tmux with 'focus-events' on, two clients on ptys, a real
adopted Vim in insert mode. A focus report (CSI I / CSI O) written to a
client's pty is what its terminal sends when its window gains or loses
focus."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from e2e_harness import AttachedClient, E2EFollower, attached_client, e2e_world
from test_integration_adopted_vim_typing import (
    _adopted_state,
    _edit_answered,
    _hook_log,
    _question_seconds,
    _settled,
    _type_half,
    _users_vim,
)

pytestmark = pytest.mark.integration

_FOCUS_IN = b"\x1b[I"
_FOCUS_OUT = b"\x1b[O"


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _clients(world: E2EFollower) -> list[tuple[str, int, str]]:
    """(pid, activity, flags) of every client, in list-clients order."""
    listed = world.tmux("list-clients", "-F", "#{client_pid} #{client_activity} #{client_flags}")
    clients = []
    for line in listed.stdout.splitlines():
        pid, activity, flags = line.split(" ", 2)
        clients.append((pid, int(activity), flags))
    return clients


def _tie_with_focus_on(world: E2EFollower, focused: AttachedClient, other: AttachedClient) -> None:
    """Report focus out on `other` and in on `focused` until both clients
    show the same activity second and only `focused` is marked focused."""
    for _attempt in range(10):
        other.type(_FOCUS_OUT)
        focused.type(_FOCUS_IN)
        try:
            world.wait_until(
                lambda: (
                    {
                        pid: "focused" in flags.split(",")
                        for pid, _activity, flags in _clients(world)
                    }
                    == {str(focused.pid): True, str(other.pid): False}
                ),
                "the focus reports",
                timeout=2.0,
            )
        except AssertionError:
            continue
        if len({activity for _pid, activity, _flags in _clients(world)}) == 1:
            return
    raise AssertionError(f"no activity tie with one focused client: {_clients(world)}")


def test_a_tie_shows_the_question_on_the_focused_client(world: E2EFollower) -> None:
    _question_seconds(world, 4)
    world.tmux("set-option", "-g", "focus-events", "on")
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\ntwo\n")
    pane_id, observed = _users_vim(world, users_file, claudes_file, showmode=True)
    _adopted_state(world, pane_id, claudes_file)
    _type_half(world, pane_id, observed, users_file)
    try:
        with attached_client(world) as first, attached_client(world) as second:
            # The focused client is the one listed LAST, so "the first
            # listed" (the old rule) is the other one.
            listed = [pid for pid, _activity, _flags in _clients(world)]
            by_pid = {str(first.pid): first, str(second.pid): second}
            focused, other = by_pid[listed[-1]], by_pid[listed[0]]
            _tie_with_focus_on(world, focused, other)
            _seconds, state = _edit_answered(world, claudes_file, "y", client=focused)
        log = _hook_log(world)
        assert "Traceback" not in log, log
        assert "(in insert mode): taken over (answered y)" in log, log
        report = _settled(world, observed)
        assert report["claudes"] == "one|TWO", report
        assert state.stale_files == ()
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")
