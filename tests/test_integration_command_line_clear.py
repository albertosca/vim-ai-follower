"""No follower line stays on a REAL Vim's command line once a call returns.

Vim leaves a typed Ex command on its bottom row until something redraws it,
so the lock line after every Read (`:setlocal readonly nomodifiable`) stayed
there, and in a pane wide enough for a call to fit one row, every dispatcher
call did too (review I1 of T23, measured 2026-10-17: 12 of 12 calls at 200
columns). Checked with capture-pane once two captures 300 ms apart agree
after each call returns (see `_lingering`), in a
narrow (the follower's 49 columns) and a wide pane, dedicated and adopted."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from helpers import load_scaled
from test_integration_handoff_write import _prove_private

from vim_ai_follower import control
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import compute_edit_script
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

BEFORE = "x = 1\ny = 2\n"
AFTER = "x = 1\nz = 3\ny = 2\n"
LONG_AFTER = BEFORE + "".join(f"line_{index} = {index}\n" for index in range(40))
FOLLOWER_MARKS = ("VafFollower", "exists('", "endif", "setlocal", "paste", "redraw")


def _tmux(*arguments: str) -> str:
    return subprocess.run(
        ["tmux", *arguments], capture_output=True, text=True, check=True
    ).stdout.strip()


def _follower_rows(pane: str) -> list[str]:
    """The follower text on the pane's last two rows (a call spans two at
    49 columns), if any."""
    return [
        row
        for row in _tmux("capture-pane", "-p", "-t", pane).splitlines()[-2:]
        if row.startswith(":") or any(mark in row for mark in FOLLOWER_MARKS)
    ]


def _lingering(pane: str) -> list[str]:
    """The follower text that STAYS on the bottom rows once Vim has run the
    call, if any. A line Vim has not run yet is on screen too, and on a
    loaded machine it may still be there at any fixed time (N-D: this test
    failed at load 30 with a single capture 300 ms after the call), so the
    rows count as clear only when two captures 300 ms apart both are; a line
    that has run and stayed (the bug) is never clear, and fails at the
    deadline with what it showed last."""
    deadline = time.monotonic() + load_scaled(5.0)
    clear_in_a_row = 0
    rows: list[str] = []
    while time.monotonic() < deadline:
        time.sleep(0.3)
        rows = _follower_rows(pane)
        clear_in_a_row = 0 if rows else clear_in_a_row + 1
        if clear_in_a_row == 2:
            return []
    return rows


def _follower(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    width: int,
    adopted: bool,
) -> tuple[str, str, TmuxVimFollower]:
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    (root / "mine.py").write_text("mine = 0\n")
    session = f"w{width}"
    _tmux("new-session", "-d", "-s", session, "-x", str(width), "-y", "30", "-c", str(root))
    pane = _tmux(
        "new-window",
        "-t",
        session,
        "-c",
        str(root),
        "-e",
        f"HOME={home}",
        "-P",
        "-F",
        "#{pane_id}",
        "sh",
        "-c",
        "sleep 0.3; exec vim mine.py",
    )
    assert _tmux("display-message", "-p", "-t", pane, "#{pane_width}") == str(width)
    deadline = time.monotonic() + load_scaled(10.0)
    while "mine = 0" not in _tmux("capture-pane", "-p", "-t", pane):
        assert time.monotonic() < deadline, "setup failed: Vim never showed mine.py"
        time.sleep(0.05)
    window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
    FollowerState.set(
        window_id,
        backend="tmux",
        target=pane,
        adopted=adopted,
        speed="instant",
        shown_any=adopted,
    )
    return pane, window_id, TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id=window_id)


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
@pytest.mark.parametrize("width", [49, 200])
def test_no_follower_line_stays_on_the_command_line(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    width: int,
    adopted: bool,
) -> None:
    _prove_private()
    root = Path(os.path.realpath(tmp_path))
    pane, window_id, follower = _follower(tmux_session, monkeypatch, root, width, adopted)
    left: dict[str, list[str]] = {}

    def check(label: str) -> None:
        rows = _lingering(pane)
        if rows:
            left[label] = rows

    path = root / "f.py"
    path.write_text(BEFORE)
    follower.show_fresh(str(path), BEFORE, in_new_tab=adopted)
    control.clear_animating(window_id)
    check("show_fresh")
    path.write_text(AFTER)
    follower.probe_buffer(str(path), BEFORE)
    check("probe_buffer")
    follower.apply_edit(str(path), compute_edit_script(BEFORE, AFTER), BEFORE)
    control.clear_animating(window_id)
    check("apply_edit")
    follower.ensure_showing(str(path))
    check("ensure_showing")
    follower.goto_line(2)
    check("goto_line")

    # An interrupted animation: the user's 'paste' goes back (and, adopted,
    # their readonly), and that is the last line the follower types.
    slow = TmuxVimFollower(pane_id=pane, pace_seconds=0.05, window_id=window_id)
    path.write_text(LONG_AFTER)
    assert control.mark_animating(window_id)
    results: list[object] = []
    worker = threading.Thread(
        target=lambda: results.append(
            slow.apply_edit(str(path), compute_edit_script(AFTER, LONG_AFTER), AFTER)
        )
    )
    worker.start()
    time.sleep(0.6)
    assert control.request_interrupt(window_id)
    worker.join(timeout=30.0)
    control.clear_animating(window_id)
    assert [getattr(result, "outcome", None) for result in results] == ["interrupted"]
    check("apply_edit interrupted")
    follower.hand_over()
    check("hand_over")
    follower.close_tab(str(path))
    check("close_tab")
    assert left == {}
