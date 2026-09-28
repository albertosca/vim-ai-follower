"""A `readonly` the USER set survives the follower's animation (tmux backend),
against a real tmux+vim at the follower pane's real 49 columns.

The unlock around every animation says `noreadonly` (typing into a readonly
buffer raises W10, a hit-enter prompt at 49 columns that eats the animation),
and the completion relock's `:e!` resets 'readonly' too. So a buffer the user
had opened with `:view` came back writable, and on the interrupted path it
stayed writable. The backend now records, at the unlock, whether the readonly
was the user's, and puts it back on every exit.

"The user's" is the whole difficulty: the follower sets 'readonly' itself
(ensure_showing's lock, show_fresh's relock), and restoring THAT on an
interrupt would turn the hand-off's ":w releases" into E45. The follower's own
locks stamp `b:changedtick`; any reload since (a user `:view`) moves the tick,
so a readonly with a stale stamp is the user's.

The buffer and its options are read back with writefile(), never capture-pane.
"""

from __future__ import annotations

import io
import json
import subprocess
import threading
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

from vim_ai_follower import cli, control
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

BEFORE = "a = 1\nb = 2\n"
AFTER = "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\nf = 6\ng = 7\nh = 8\n"


def _keys(pane: str, *commands: str) -> None:
    for command in commands:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", command], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)


def _options(
    pane: str, target: Path, tmp_path: Path, wait_until: Callable[..., bool]
) -> tuple[str, str]:
    """(&readonly, &modifiable) of `target`'s real buffer, dumped by Vim
    behind a sentinel so a stale or half-written dump is never read."""
    dump = tmp_path / "opts.txt"
    dump.unlink(missing_ok=True)
    _keys(
        pane,
        f":call writefile(['OPTS', string(getbufvar(bufnr('{target}'), '&readonly')),"
        f" string(getbufvar(bufnr('{target}'), '&modifiable'))], '{dump}')",
    )
    assert wait_until(lambda: dump.exists() and dump.read_text().startswith("OPTS\n"), timeout=5.0)
    lines = dump.read_text().splitlines()
    return lines[1], lines[2]


def _edit(monkeypatch: pytest.MonkeyPatch, log: Path, target: Path) -> None:
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text(AFTER)
    _hook(monkeypatch, "post", "Edit", target)


def test_a_view_the_user_ran_on_a_followed_file_is_readonly_after_the_edit(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The follower showed the file (its relock stamped its own readonly),
    then the user ran `:view` on it: that readonly is now the user's."""
    log, pane, window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    _write(monkeypatch, log, wait_until, target, BEFORE)
    FollowerState.update(window_id, adopted=True)
    _keys(pane, f":view {target}")
    assert wait_until(lambda: _options(pane, target, tmp_path, wait_until)[0] == "1", 5.0)

    _edit(monkeypatch, log, target)

    assert wait_until(lambda: _last_state(log, target.name) == AFTER.splitlines(), timeout=15.0)
    assert len(_states(log, target.name)) > 1, "the edit never animated"
    assert _options(pane, target, tmp_path, wait_until)[0] == "1"


def test_a_file_the_user_opened_with_view_is_readonly_after_the_edit(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The user opened the file in their own (adopted) Vim; the follower
    never showed it before."""
    log, pane, window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    _write(monkeypatch, log, wait_until, tmp_path / "seed.py", "seed = 1\n")
    FollowerState.update(window_id, adopted=True)
    target = tmp_path / "b.py"
    target.write_text(BEFORE)
    _keys(pane, f":tab view {target}")
    assert wait_until(lambda: _options(pane, target, tmp_path, wait_until) == ("1", "1"), 5.0)

    _edit(monkeypatch, log, target)

    assert wait_until(lambda: _last_state(log, target.name) == AFTER.splitlines(), timeout=15.0)
    assert len(_states(log, target.name)) > 1, "the edit never animated"
    assert _options(pane, target, tmp_path, wait_until)[0] == "1"


def _interrupt_mid_edit(
    monkeypatch: pytest.MonkeyPatch,
    log: Path,
    window_id: str,
    target: Path,
    wait_until: Callable[..., bool],
) -> threading.Thread:
    """Run the Edit's post hook in a thread and interrupt it mid-typing.
    Returns the thread, parked in the hand-off wait."""
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text(AFTER)
    body = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(target)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    thread = threading.Thread(target=lambda: cli.main(["hook", "post"]))
    thread.start()
    assert wait_until(lambda: control.animating_state(window_id) == "running", timeout=10.0)
    assert wait_until(lambda: len(_states(log, target.name)) >= 2, timeout=15.0)
    control.request_interrupt(window_id)
    assert wait_until(lambda: control.animating_state(window_id) == "handoff", timeout=15.0)
    pending = control.load_pending_animation(window_id)
    assert pending is not None
    assert pending.partial is not None
    partial = pending.partial.splitlines()
    assert partial != AFTER.splitlines(), "interrupt landed after the edit finished"
    assert wait_until(lambda: _last_state(log, target.name) == partial, timeout=5.0)
    return thread


def test_an_interrupted_edit_hands_back_the_users_readonly(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    log, pane, window_id = _observed_follower(
        tmux_session, monkeypatch, tmp_path, wait_until, "--speed", "lento"
    )
    _write(monkeypatch, log, wait_until, tmp_path / "seed.py", "seed = 1\n")
    FollowerState.update(window_id, adopted=True)
    target = tmp_path / "c.py"
    target.write_text(BEFORE)
    _keys(pane, f":tab view {target}")
    assert wait_until(lambda: _options(pane, target, tmp_path, wait_until) == ("1", "1"), 5.0)

    thread = _interrupt_mid_edit(monkeypatch, log, window_id, target, wait_until)
    try:
        assert _options(pane, target, tmp_path, wait_until) == ("1", "1")
    finally:
        target.write_text("the user's own version\n")
        thread.join(timeout=15.0)
    assert not thread.is_alive()


def test_an_interrupt_never_hands_back_the_followers_own_readonly(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The file was shown by a Read, whose lock sets 'readonly'. That readonly
    is the follower's: bringing it back at the hand-off would make the cue's
    `:w` fail with E45."""
    log, pane, window_id = _observed_follower(
        tmux_session, monkeypatch, tmp_path, wait_until, "--speed", "lento"
    )
    target = tmp_path / "d.py"
    _write(monkeypatch, log, wait_until, target, BEFORE)
    _hook(monkeypatch, "post", "Read", target)
    assert wait_until(lambda: _options(pane, target, tmp_path, wait_until) == ("1", "0"), 5.0)

    thread = _interrupt_mid_edit(monkeypatch, log, window_id, target, wait_until)
    try:
        assert _options(pane, target, tmp_path, wait_until)[0] == "0"
    finally:
        target.write_text("the user's own version\n")
        thread.join(timeout=15.0)
    assert not thread.is_alive()
