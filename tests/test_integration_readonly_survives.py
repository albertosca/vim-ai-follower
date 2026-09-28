"""A `readonly` the USER set in an ADOPTED Vim survives the follower's
animation (tmux backend), against a real tmux+vim at the follower pane's real
49 columns; and the hand-off cue asks for `:w!` exactly when it does.

The unlock around every animation says `noreadonly` (typing into a readonly
buffer raises W10, a hit-enter prompt at 49 columns that eats the animation),
and the completion relock's `:e!` resets 'readonly' too. So a buffer the user
had opened with `:view` came back writable, and on the interrupted path it
stayed writable.

"The user's" is the whole difficulty: the follower sets 'readonly' itself
(ensure_showing's lock, show_fresh's relock), and handing THAT back at an
interrupt makes the cue's `:w` fail with E45 and leaves Claude blocked. The
follower marks its own readonly in `b:vaf_ro_ours`, a buffer variable that a
reload (`:checktime` under 'autoread', a formatter's rewrite) keeps, and notes
the user's readonly before every step of its own that changes the option.

A DEDICATED follower's readonly is never the user's: it is never restored.

The buffer and its options are read back with writefile(), never capture-pane.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import time
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
from vim_ai_follower.hooks import HANDOFF_CUE, HANDOFF_CUE_READONLY
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.integration

BEFORE = "a = 1\nb = 2\n"
AFTER = "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\nf = 6\ng = 7\nh = 8\n"


def _prove_private() -> None:
    """Every tmux call below must reach the fixture's throwaway server."""
    assert "TMUX" not in os.environ
    socket = subprocess.run(
        ["tmux", "display-message", "-p", "#{socket_path}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert socket.startswith(os.path.realpath(os.environ["TMUX_TMPDIR"])), socket


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


def _adopted_with_user_view(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    name: str,
    *start_args: str,
) -> tuple[Path, str, str, Path]:
    """An adopted follower whose Vim has `name` open with `:tab view`: the
    user's own readonly, on a file the follower never showed."""
    _prove_private()
    log, pane, window_id = _observed_follower(
        tmux_session, monkeypatch, tmp_path, wait_until, *start_args
    )
    _write(monkeypatch, log, wait_until, tmp_path / "seed.py", "seed = 1\n")
    FollowerState.update(window_id, adopted=True)
    target = tmp_path / name
    target.write_text(BEFORE)
    _keys(pane, f":tab view {target}")
    assert wait_until(lambda: _options(pane, target, tmp_path, wait_until) == ("1", "1"), 5.0)
    return log, pane, window_id, target


def test_a_file_the_user_opened_with_view_is_readonly_after_the_edit(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    log, pane, _, target = _adopted_with_user_view(
        tmux_session, monkeypatch, tmp_path, wait_until, "b.py"
    )

    _edit(monkeypatch, log, target)

    assert wait_until(lambda: _last_state(log, target.name) == AFTER.splitlines(), timeout=15.0)
    assert len(_states(log, target.name)) > 1, "the edit never animated"
    assert _options(pane, target, tmp_path, wait_until)[0] == "1"


def test_a_read_before_the_edit_keeps_the_users_view(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The normal order (Claude must Read before it Edits): the Read's lock
    puts the follower's readonly over the user's, which must still come back
    after the Edit."""
    log, pane, _, target = _adopted_with_user_view(
        tmux_session, monkeypatch, tmp_path, wait_until, "b.py"
    )
    _hook(monkeypatch, "post", "Read", target)
    assert wait_until(lambda: _options(pane, target, tmp_path, wait_until) == ("1", "0"), 5.0)

    _edit(monkeypatch, log, target)

    assert wait_until(lambda: _last_state(log, target.name) == AFTER.splitlines(), timeout=15.0)
    assert _options(pane, target, tmp_path, wait_until)[0] == "1"


def _interrupt_mid_edit(
    monkeypatch: pytest.MonkeyPatch,
    log: Path,
    window_id: str,
    target: Path,
    wait_until: Callable[..., bool],
) -> threading.Thread:
    """Run the Edit's post hook in a thread and interrupt it mid-typing.
    Returns the thread, parked in the hand-off wait. On any failure the hook
    is released first, so it never outlives the test."""
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text(AFTER)
    body = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(target)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    thread = threading.Thread(target=lambda: cli.main(["hook", "post"]))
    thread.start()
    try:
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
    except BaseException:
        target.write_text("released by the test\n")
        thread.join(timeout=15.0)
        raise
    return thread


def _release(target: Path, thread: threading.Thread) -> None:
    target.write_text("the user's own version\n")
    thread.join(timeout=15.0)
    assert not thread.is_alive()


def test_an_interrupted_edit_hands_back_the_users_readonly_and_asks_for_w_bang(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    log, pane, window_id, target = _adopted_with_user_view(
        tmux_session, monkeypatch, tmp_path, wait_until, "c.py", "--speed", "lento"
    )

    thread = _interrupt_mid_edit(monkeypatch, log, window_id, target, wait_until)
    try:
        assert _options(pane, target, tmp_path, wait_until) == ("1", "1")
        assert wait_until(lambda: TmuxPane(pane_id=pane).title() == HANDOFF_CUE_READONLY, 5.0), (
            TmuxPane(pane_id=pane).title()
        )
    finally:
        _release(target, thread)


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_an_interrupt_never_hands_back_the_followers_own_readonly(
    adopted: bool,
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The file was shown by a Read, whose lock sets 'readonly'. That readonly
    is the follower's: bringing it back at the hand-off would make the cue's
    `:w` fail with E45."""
    _prove_private()
    log, pane, window_id = _observed_follower(
        tmux_session, monkeypatch, tmp_path, wait_until, "--speed", "lento"
    )
    target = tmp_path / "d.py"
    if adopted:  # from the start, as `start` adopts: the Write is adopted too
        FollowerState.update(window_id, adopted=True)
    _write(monkeypatch, log, wait_until, target, BEFORE)
    _hook(monkeypatch, "post", "Read", target)
    assert wait_until(lambda: _options(pane, target, tmp_path, wait_until) == ("1", "0"), 5.0)

    thread = _interrupt_mid_edit(monkeypatch, log, window_id, target, wait_until)
    try:
        assert _options(pane, target, tmp_path, wait_until)[0] == "0"
        assert wait_until(lambda: TmuxPane(pane_id=pane).title() == HANDOFF_CUE, 5.0), TmuxPane(
            pane_id=pane
        ).title()
    finally:
        _release(target, thread)


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_a_reload_under_autoread_never_makes_the_followers_readonly_the_users(
    adopted: bool,
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """With 'autoread' (Alberto's config sets it), `:checktime` (FocusGained)
    re-reads a file a formatter rewrote. The reload keeps the follower's
    readonly and moves the changedtick, which is all a tick-based guess saw:
    the follower's readonly came back at the hand-off and `:w` failed E45."""
    _prove_private()
    log, pane, window_id = _observed_follower(
        tmux_session, monkeypatch, tmp_path, wait_until, "--speed", "lento"
    )
    _keys(pane, ":set autoread")
    target = tmp_path / "e.py"
    if adopted:  # from the start, as `start` adopts: the Write is adopted too
        FollowerState.update(window_id, adopted=True)
    _write(monkeypatch, log, wait_until, target, "a = 1\n")
    _hook(monkeypatch, "post", "Read", target)
    assert wait_until(lambda: _options(pane, target, tmp_path, wait_until) == ("1", "0"), 5.0)
    time.sleep(1.1)  # mtimes may compare in whole seconds
    target.write_text(BEFORE)  # a formatter rewrote it
    _keys(pane, ":checktime")
    assert wait_until(lambda: _last_state(log, target.name) == BEFORE.splitlines(), timeout=5.0)
    assert _options(pane, target, tmp_path, wait_until) == ("1", "0")

    thread = _interrupt_mid_edit(monkeypatch, log, window_id, target, wait_until)
    try:
        assert _options(pane, target, tmp_path, wait_until)[0] == "0"
        assert wait_until(lambda: TmuxPane(pane_id=pane).title() == HANDOFF_CUE, 5.0), TmuxPane(
            pane_id=pane
        ).title()
    finally:
        _release(target, thread)
