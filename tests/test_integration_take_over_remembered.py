"""A "no" or a timeout to the take-over question is remembered (final review
I6, Alberto's decision: until the mode changes): while the same adopted Vim
stays in the same typing mode, the next hooks skip without asking again.
Each question used to cost up to its whole time and take a key from
whatever the user was typing, on every Edit and Read.

Real CLI, real tmux, real Vims, `HOME` isolated by the e2e harness; the
helpers are the adopted typing tests'."""

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
    _type_half,
    _users_vim,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _skipped_lines(world: E2EFollower) -> list[str]:
    return [line for line in _hook_log(world).splitlines() if "is waiting on its user" in line]


def test_a_no_is_remembered_until_the_mode_changes(world: E2EFollower) -> None:
    _question_seconds(world, 3)
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\ntwo\n")
    pane_id, observed = _users_vim(world, users_file, claudes_file, showmode=False)
    _adopted_state(world, pane_id, claudes_file)
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
    _type_half(world, pane_id, observed, users_file)
    try:
        _edit_answered(world, claudes_file, "n")
        # Still in insert mode: skipped at once, nothing asked.
        claudes_file.write_text("one\ntwo\n")
        seconds, state = _edit_answered(world, claudes_file, "unasked")
        skipped = _skipped_lines(world)
        assert len(skipped) == 2, _hook_log(world)
        assert "not taken over (answered no)" in skipped[0], skipped
        assert "not taken over (answered no earlier in this mode)" in skipped[1], skipped
        assert seconds < 2.5, f"the hook waited {seconds:.1f} s"
        assert state.stale_files == (os.path.realpath(claudes_file),)
        report = _observation(world, observed, lambda _report: True, "an observation", 5.0)
        assert (report["mode"], report["lines"]) == ("i", "aaahalf"), report

        # Another typing mode: asked again.
        world.tmux("send-keys", "-t", pane_id, "Escape")
        world.tmux("send-keys", "-t", pane_id, "v")
        _observation(world, observed, lambda report: report["mode"] == "v", "visual mode", 10.0)
        claudes_file.write_text("one\ntwo\n")
        _edit_answered(world, claudes_file, "n")
        skipped = _skipped_lines(world)
        assert len(skipped) == 3, _hook_log(world)
        assert "(in visual mode), not taken over (answered no)" in skipped[2], skipped
        assert "Traceback" not in _hook_log(world)
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")
