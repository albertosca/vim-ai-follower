"""A `y` to the take-over question does not take over a Vim that is still
typing after it (final review M3): with a CTRL-R pending in insert mode, the
follower's CTRL-\\ CTRL-N is not a mode switch — CTRL-R drops the CTRL-\\
and CTRL-N then completes a word, leaving Vim in insert completion (`ic`,
measured on Vim 9.2) — and the Escape pair every navigation starts with
kept the completed word in the user's buffer and went on to type there.

The hook now reads the mode again after the yes and, still typing after
2 s, skips the edit and says so. Real CLI, real tmux, real Vims, `HOME`
isolated by the e2e harness; the helpers are the adopted typing tests'."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from e2e_harness import E2EFollower, e2e_world, payload
from test_integration_adopted_vim_typing import (
    _adopted_state,
    _edit_answered,
    _hook_log,
    _observation,
    _observed,
    _question_seconds,
    _users_vim,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def test_a_yes_with_a_ctrl_r_pending_skips_instead_of_typing(world: E2EFollower) -> None:
    _question_seconds(world, 3)
    users_file = world.workdir / "a.txt"
    users_file.write_text("abcdef\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\ntwo\n")
    pane_id, observed = _users_vim(world, users_file, claudes_file, showmode=False)
    _adopted_state(world, pane_id, claudes_file)
    # A first hook in Normal mode sources the script and its mode file.
    read = world.cli("hook", "post", stdin=payload("Read", claudes_file))
    assert read.returncode == 0, read.stderr
    world.wait_until(
        lambda: (_observed(observed) or {}).get("buffer") == os.path.realpath(claudes_file),
        "the Read's navigation",
        timeout=10.0,
    )
    world.tmux("send-keys", "-t", pane_id, "-l", ":tabfirst\r")
    world.wait_until(
        lambda: (_observed(observed) or {}).get("buffer") == os.path.realpath(users_file),
        "the user back in their file",
        timeout=10.0,
    )
    world.tmux("send-keys", "-t", pane_id, "-l", "A abc")
    world.tmux("send-keys", "-t", pane_id, "C-r")
    _observation(
        world,
        observed,
        lambda report: report["lines"] == "abcdef abc" and report["mode"] == "i",
        "the user's text with a CTRL-R pending",
        10.0,
    )
    try:
        _seconds, state = _edit_answered(world, claudes_file, "y")
        log = _hook_log(world)
        assert "Traceback" not in log, log
        skipped = [line for line in log.splitlines() if "is waiting on its user" in line]
        assert len(skipped) == 1, log
        assert "(in insert mode), answered y but it was still in insert mode" in skipped[0]
        assert "CTRL-R" in skipped[0], skipped
        assert state.stale_files == (os.path.realpath(claudes_file),)
        report = _observation(world, observed, lambda _report: True, "an observation", 5.0)
        # The completion stays the user's to keep or cancel; nothing else
        # was typed, and Claude's buffer was not touched.
        assert report["mode"].startswith("i"), report
        assert report["buffer"] == os.path.realpath(users_file), report
        assert report["claudes"] == "one|two", report
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")
