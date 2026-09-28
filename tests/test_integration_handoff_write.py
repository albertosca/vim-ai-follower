"""The hand-off cue says ":w releases": on a file a Write created, plain `:w`
must work, against a real tmux+vim at the follower pane's 49 columns.

show_fresh names its buffer with `:file`, and renaming a buffer onto a path
marks it "not edited": Vim then refuses a plain `:w` over the existing file
with `E13: File exists (add ! to override)` (found recording the demo). Only a
completed animation escaped it, through the relock's `:e!`. The rename now
reads the file into the buffer and clears it on the same command line, which
Vim never redraws in between, so nothing is shown before it is typed.

Reading also gives the buffer the file's timestamp, so the protection a plain
`:w` should keep is there: a file changed outside Claude since the read still
gets Vim's "changed since reading it" question.
"""

from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_edit_no_reload import _MARK, _last_state, _observed_follower, _states

from vim_ai_follower import cli, control

pytestmark = pytest.mark.integration


def _screen(pane: str) -> str:
    """The rendered screen: right for a prompt, never for buffer content."""
    return subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", pane], capture_output=True, text=True, check=True
    ).stdout


CONTENT = "".join(f"line_{n} = {n}\n" for n in range(1, 13))


def _keys(pane: str, *commands: str) -> None:
    for command in commands:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", command], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)


def _interrupted_write(
    monkeypatch: pytest.MonkeyPatch,
    log: Path,
    window_id: str,
    target: Path,
    wait_until: Callable[..., bool],
) -> tuple[threading.Thread, list[str]]:
    """Write `target` through the hooks and interrupt the retype mid-way.
    Returns the post hook's thread (parked in the hand-off wait) and the
    partial the user was handed."""
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    body = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    assert cli.main(["hook", "pre"]) == 0
    target.write_text(CONTENT)
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    thread = threading.Thread(target=lambda: cli.main(["hook", "post"]))
    thread.start()
    try:
        full = CONTENT.splitlines()
        assert wait_until(lambda: control.animating_state(window_id) == "running", timeout=10.0)
        # Some lines typed, not all: the interrupt must land mid-retype.
        assert wait_until(
            lambda: any(2 <= len(s) < len(full) for s in _states(log, target.name)),
            timeout=15.0,
        )
        control.request_interrupt(window_id)
        assert wait_until(lambda: control.animating_state(window_id) == "handoff", timeout=15.0)
        # The read that fixes E13 must never put the finished file on screen.
        assert full not in _states(log, target.name)
        pending = control.load_pending_animation(window_id)
        assert pending is not None
        assert pending.partial is not None
        partial = pending.partial.splitlines()
        assert partial != full, "interrupt landed after the write finished"
        assert wait_until(lambda: _last_state(log, target.name) == partial, timeout=5.0)
    except BaseException:
        # Never leave the hook parked in the hand-off wait: pytest would hang
        # on the thread at exit.
        target.write_text("released by the test\n")
        thread.join(timeout=15.0)
        raise
    return thread, partial


def test_plain_w_saves_the_handed_over_buffer_and_releases_claude(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    log, pane, window_id = _observed_follower(
        tmux_session, monkeypatch, tmp_path, wait_until, "--speed", "lento"
    )
    target = tmp_path / "new.py"
    thread, partial = _interrupted_write(monkeypatch, log, window_id, target, wait_until)
    try:
        _keys(pane, ":w")
        assert wait_until(lambda: target.read_text().splitlines() == partial, timeout=10.0), (
            _screen(pane)
        )
        thread.join(timeout=15.0)
        assert not thread.is_alive(), "the save did not release Claude"
    finally:
        if thread.is_alive():
            target.write_text("released by the test\n")
            thread.join(timeout=15.0)
    assert not thread.is_alive()


def test_plain_w_still_warns_when_the_file_changed_outside_since(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    log, pane, window_id = _observed_follower(
        tmux_session, monkeypatch, tmp_path, wait_until, "--speed", "lento"
    )
    target = tmp_path / "new.py"
    thread, _ = _interrupted_write(monkeypatch, log, window_id, target, wait_until)
    # Something else rewrites the file (mtimes are compared in whole seconds
    # on some filesystems). The hand-off reads any change as the user's save
    # and releases Claude, which is fine: this test is about Vim's `:w`.
    time.sleep(1.1)
    target.write_text("changed outside\n")
    thread.join(timeout=15.0)
    assert not thread.is_alive()

    _keys(pane, ":w")
    assert wait_until(lambda: "changed since reading" in _screen(pane), timeout=10.0), _screen(pane)
    subprocess.run(["tmux", "send-keys", "-t", pane, "n"], check=True)
    time.sleep(0.5)
    assert target.read_text() == "changed outside\n"
