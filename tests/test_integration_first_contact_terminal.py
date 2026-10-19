"""A first contact with an adopted Vim whose user is in a terminal window's
job (final review M4). Terminal-Job mode draws no mode word and no hook has
sourced the script yet, so there is no mode file: typing_mode read "not
typing", and the define line and the Escape pair went into the user's shell
— on every later contact too, since the script that would write the mode
file was never sourced.

With no trusted mode file and a terminal job running under the Vim (a child
on a pty of its own), the hook now asks, as for a known typing mode; a `y`
sends CTRL-\\ CTRL-N first. Real CLI, real tmux, real Vims, `HOME` isolated
by the e2e harness; the helpers are the adopted typing tests'."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from e2e_harness import E2EFollower, e2e_world
from test_integration_adopted_vim_typing import (
    _adopted_state,
    _edit_answered,
    _hook_log,
    _observed,
    _question_seconds,
    _settled,
    _users_vim,
)

pytestmark = pytest.mark.integration

_UNKNOWN = "in an unknown mode (a terminal job runs in it, and its mode was never recorded)"


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


@pytest.mark.parametrize("answer", ["n", "y"])
def test_a_first_contact_with_a_terminal_job_asks_before_typing(
    world: E2EFollower, answer: str
) -> None:
    _question_seconds(world, 3)
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\ntwo\n")
    pane_id, observed = _users_vim(world, users_file, claudes_file, showmode=True)
    _adopted_state(world, pane_id, claudes_file)
    world.tmux("send-keys", "-t", pane_id, "-l", ":term\r")
    world.wait_until(
        lambda: (_observed(observed) or {}).get("mode") == "t",
        "the user in a terminal window's job",
        timeout=10.0,
    )
    assert not list(world.cache_dir.glob("mode-*.txt"))
    try:
        _seconds, state = _edit_answered(world, claudes_file, answer)
        log = _hook_log(world)
        assert "Traceback" not in log, log
        report = _settled(world, observed)
        assert "exists(" not in report["terminals"], report
        assert "VafFollower" not in report["terminals"], report
        if answer == "n":
            skipped = [line for line in log.splitlines() if "is waiting on its user" in line]
            assert len(skipped) == 1, log
            assert f"({_UNKNOWN}), not taken over (answered no)" in skipped[0], skipped
            assert report["mode"] == "t", report
            assert state.stale_files == (os.path.realpath(claudes_file),)
        else:
            assert f"({_UNKNOWN}): taken over (answered y)" in log, log
            assert report["buffer"] == os.path.realpath(claudes_file), report
            assert report["claudes"] == "one|TWO", report
            assert state.stale_files == ()
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")
