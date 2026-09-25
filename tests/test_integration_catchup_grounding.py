"""After a completed catch-up, a buffer that is not the new edit's base is
GROUNDED on disk (a real tmux + Vim at the follower's 49 columns).

The pre-edit catch-up replays an earlier edit's remainder and ends at that
edit's result WITHOUT re-reading disk (disk already holds the new edit). When
something outside Claude also rewrote the file in between (a formatter hook,
`sed -i`, a checkout), the base probe then answers "differs". Left alone,
that buffer is modified (&modified=1) with text only the follower typed,
while disk holds the formatted file plus the new edit: a `:checktime`
(FocusGained with 'autoread') raises the blocking W12 dialog, and a `:w`
asks "file changed since reading" and, on y, writes the stale text over
Claude's and the formatter's edits (reproduced 2026-09-25 by the B2
re-reviewer).

Ruling (controller, 2026-09-25): the buffer the catch-up just produced holds
only follower text, so it is reloaded from disk — Claude's finished file is
shown, with no animation, no cue and no stale mark — in adopted and
dedicated mode alike.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_edit_no_reload import (
    _MARK,
    _hook,
    _last_state,
    _observed_follower,
    _states,
    _write,
)

from vim_ai_follower import control
from vim_ai_follower.diff import compute_edit_script
from vim_ai_follower.hooks import BASE_DIFFERS_CUE, PROBE_UNKNOWN_CUE
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.integration

WRITTEN = "x = 1\ny = 2\n"  # what the follower animated
KILLED = "x = 1\ny = 3\n"  # the killed Edit's write: where the catch-up ends
FORMATTED = "x = 1\ny = 3\n# fmt\n"  # an outside change: the new edit's base
FINISHED = "x = 1\ny = 3\n# fmt\nz = 4\n"  # the new Edit


def _keys(pane: str, *commands: str) -> None:
    for command in commands:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", command], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)


def _screen(pane: str) -> str:
    return subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", pane], capture_output=True, text=True, check=True
    ).stdout


def _width(pane: str) -> int:
    out = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, "#{pane_width}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def _dump(
    pane: str, target: Path, tmp_path: Path, wait_until: Callable[..., bool]
) -> tuple[str, list[str]]:
    """(&modified, lines) of target's real buffer, behind a sentinel line."""
    dump = tmp_path / "dump.txt"
    dump.unlink(missing_ok=True)
    _keys(
        pane,
        f":call writefile(['DUMP', getbufvar(bufnr('{target}'), '&modified')]"
        f" + getbufline(bufnr('{target}'), 1, '$'), '{dump}')",
    )
    assert wait_until(
        lambda: dump.exists() and dump.read_text().startswith("DUMP\n"), timeout=5.0
    ), _screen(pane)
    lines = dump.read_text().splitlines()
    return lines[1], lines[2:]


@pytest.mark.parametrize("adopted", [True, False], ids=["adopted", "dedicated"])
def test_a_caught_up_buffer_an_outside_change_left_behind_is_grounded_on_disk(
    adopted: bool,
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    log, pane, window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    assert _width(pane) == 49
    target = tmp_path / "a.py"
    _write(monkeypatch, log, wait_until, target, WRITTEN)
    if adopted:
        FollowerState.update(window_id, adopted=True)
    # A killed Edit (y = 2 -> y = 3) left its remainder pending; the buffer
    # is untouched, so the catch-up's partial probe says "holds".
    target.write_text(KILLED)
    control.save_pending_apply_edit(
        window_id,
        compute_edit_script(WRITTEN, KILLED),
        0.0,
        file_path=str(target),
        partial=WRITTEN,
    )
    target.write_text(FORMATTED)  # outside Claude

    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text(FINISHED)
    _hook(monkeypatch, "post", "Edit", target)

    after = FINISHED.splitlines()
    # Not asserted here: a buffer left alone never gets there, and the dump
    # and the prompt check below say why.
    wait_until(lambda: _last_state(log, target.name) == after, timeout=10.0)
    grounded = _dump(pane, target, tmp_path, wait_until)

    # What a FocusGained with 'autoread' runs. On an ungrounded buffer this is
    # the blocking W12 dialog; give it time to draw before reading the screen.
    _keys(pane, ":checktime")
    wait_until(lambda: "W12" in _screen(pane), timeout=2.0)
    screen = _screen(pane)
    assert "W12" not in screen, screen
    assert "Press ENTER" not in screen, screen
    assert grounded == ("0", after)
    assert _dump(pane, target, tmp_path, wait_until) == ("0", after)

    states = _states(log, target.name)
    trace = "\n".join(repr(s) for s in states)
    caught_up = KILLED.splitlines()
    assert caught_up in states, f"the catch-up never completed:\n{trace}"
    # Grounded, not retyped: from the catch-up's end on, nothing is wiped.
    assert all(s[:2] == caught_up for s in states[states.index(caught_up) :]), trace

    title = TmuxPane(pane_id=pane).title()
    assert title not in (BASE_DIFFERS_CUE, PROBE_UNKNOWN_CUE), title
    state = FollowerState.read(window_id)
    assert state is not None
    assert str(target) not in state.stale_files
    assert control.load_pending_animation(window_id) is None
