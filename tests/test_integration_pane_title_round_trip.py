"""A user's pane title survives the follower's cue byte-exact on a REAL tmux.

`StatusSurface.set_state` saves the pane title and `clear` puts it back. tmux
stores a title vis-encoded (a backslash as two) and `select-pane -T`
format-expands its argument, so a saved `a\\` came back `a\\\\` on every
save/restore and a saved `#{pane_id}` came back as the pane id (T24a review
m3). The titles are set the way a shell sets them, by an OSC 2 sequence,
which tmux neither expands nor refuses."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable

import pytest
from test_integration_handoff_write import _prove_private

from vim_ai_follower.status_surface import TmuxStatusSurface
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.integration


def _stored(pane: str) -> str:
    return subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, "#{pane_title}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.rstrip("\n")


@pytest.mark.parametrize(
    "title", ["a\\", "a\\\\", "#{pane_id}", "##", "x;", "é", "100%", "--;", "#S #", "a\\#{x}\\"]
)
def test_the_users_title_comes_back_exactly(
    tmux_session: str, wait_until: Callable[..., bool], title: str
) -> None:
    _prove_private()
    pane = subprocess.run(
        ["tmux", "split-window", "-t", tmux_session, "-P", "-F", "#{pane_id}", "/bin/sh"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["tmux", "select-pane", "-t", pane, "-T", "unset-mark"], check=True)
    TmuxPane(pane).send_line(f"printf '\\033]2;%s\\033\\\\' {shlex.quote(title)}")
    assert wait_until(lambda: _stored(pane) != "unset-mark", 5.0)
    before = _stored(pane)
    assert before == title.replace("\\", "\\\\")  # tmux stores it vis-encoded
    for _cycle in range(3):  # a doubling shows on the second cycle at the latest
        surface = TmuxStatusSurface(pane_id=pane)
        surface.set_state("Claude waiting")
        assert _stored(pane) == "Claude waiting"
        surface.clear()
        assert _stored(pane) == before, (title, before)
