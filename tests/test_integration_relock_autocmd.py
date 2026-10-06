"""The tmux follower's Vim-script steps against a hostile or unusual user Vim,
on a real tmux+vim at the follower pane's real 49 columns.

Every Vim here is hermetic (`-u NONE -i NONE`) and the user's setup is typed
into it; every state is read back with writefile(), never capture-pane, and
the read is sent with NO Escape before it, so a prompt the follower left
behind eats it and the test says so.

- An interrupt hands the buffer over with no relock, and the relock was the
  only thing that turned the follower's 'paste' off again. 'paste' is GLOBAL:
  left on, it disables every insert-mode mapping and abbreviation and all
  auto-indenting for the user, in every buffer. The hand-off gives back the
  value the user had (in an adopted Vim, possibly on).
- A buffer the user made 'nomodifiable' themselves, in an adopted Vim, gets
  that back when an animation on it is interrupted: the unlock turned it on,
  and only the readonly was ever recorded and restored (N4).
- A user autocommand that THROWS on the completion relock's `:e!` (s:relock):
  the `:e!` sat in a `try … finally` with no `catch`, so the exception ran the
  `finally` and then aborted the function before its `setlocal`, leaving the
  buffer modifiable with 'paste' on (BufReadPost), or empty and modifiable
  (BufReadPre). The old bare `:silent! e! | setlocal …` locked it in both.

Isolation: tmux_session unsets TMUX and points TMUX_TMPDIR at a private
directory; the socket is asserted before any in-process backend call."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from vim_ai_follower import control
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends import NavigationFailed
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import compute_edit_script
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

BEFORE = "a = 1\nb = 2\n"
AFTER = "a = 1\nb = 2\nc = 3\n"


def _tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=True).stdout


def _keys(pane: str, command: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", command], check=True)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)


def _assert_private_server() -> None:
    socket_path = _tmux("display-message", "-p", "#{socket_path}").strip()
    assert "TMUX" not in os.environ
    private = os.path.realpath(os.environ["TMUX_TMPDIR"])
    assert os.path.realpath(socket_path).startswith(private), socket_path


def vim_pane(tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool], setup: str) -> str:
    """A 49-column hermetic Vim, `setup` (one Ex line, or '') run in it
    first, on a line of its own: an `:autocmd` takes the rest of its line."""
    _assert_private_server()
    origin = _tmux("list-panes", "-t", tmux_session, "-F", "#{pane_id}").split()[0]
    pane = _tmux(
        "split-window", "-h", "-l", "49", "-t", origin, "-P", "-F", "#{pane_id}",
        "vim -N -u NONE -i NONE",
    ).strip()  # fmt: skip
    assert _tmux("display-message", "-p", "-t", pane, "#{pane_width}").strip() == "49"
    ready = tmp_path / "ready.txt"
    if setup:
        _keys(pane, f":{setup}")
    _keys(pane, f":call writefile(['R'], '{ready}')")
    assert wait_until(ready.exists, timeout=10.0), _tmux("capture-pane", "-p", "-t", pane)
    return pane


def follower(pane: str, *, adopted: bool, speed: str = "instant") -> TmuxVimFollower:
    FollowerState.set("@1", "tmux", pane, adopted=adopted, speed=speed)
    return TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id="@1")


def state(
    pane: str, tmp_path: Path, wait_until: Callable[..., bool], expression: str = "''"
) -> list[str]:
    """[&modifiable, &paste, `expression`] + the current buffer's lines, asked
    with NO Escape first: a prompt left in the pane would eat the command."""
    out = tmp_path / "state.txt"
    out.unlink(missing_ok=True)
    _keys(
        pane,
        f":call writefile(['S', &modifiable, &paste, {expression}] + getline(1, '$'), '{out}')",
    )
    assert wait_until(lambda: out.exists() and out.read_text().startswith("S\n"), timeout=5.0), (
        "a prompt ate the next command:\n" + _tmux("capture-pane", "-p", "-t", pane)
    )
    return out.read_text().splitlines()[1:]


@pytest.mark.parametrize(
    ("autocmd", "expected"),
    [
        ("BufReadPost * throw 'boom'", AFTER.splitlines()),
        # The throw comes before the read: `:e!` has already emptied the
        # buffer. Locked empty is the safe side (nothing typed into it).
        ("BufReadPre * throw 'boom'", [""]),
        # A plain error, swallowed by `silent!` as it always was.
        ("BufReadPost * call NoSuchFunction()", AFTER.splitlines()),
    ],
    ids=["BufReadPost-throw", "BufReadPre-throw", "plain-error"],
)
def test_the_relock_locks_whatever_a_user_autocommand_does_on_its_reload(
    autocmd: str,
    expected: list[str],
    tmux_session: str,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    pane = vim_pane(tmux_session, tmp_path, wait_until, f"autocmd {autocmd}")
    target = tmp_path / "t.py"
    target.write_text(BEFORE)
    dedicated = follower(pane, adopted=False)

    assert dedicated.show_fresh(str(target), BEFORE).outcome == "completed"
    target.write_text(AFTER)
    ops = compute_edit_script(BEFORE, AFTER)
    assert dedicated.apply_edit(str(target), ops, before=BEFORE).outcome == "completed"

    assert state(pane, tmp_path, wait_until) == ["0", "0", "", *expected]


LONG = "".join(f"line_{number} = {number}\n" for number in range(40))


def interrupted_edit(follower: TmuxVimFollower, target: Path) -> AnimationResult:
    """An apply_edit, interrupted as a keyboard interrupt would: once its
    driver has marked itself running (in this process, so the signal is
    addressed to it)."""
    target.write_text(LONG)
    results: list[AnimationResult] = []
    ops = compute_edit_script(BEFORE, LONG)
    thread = threading.Thread(
        target=lambda: results.append(follower.apply_edit(str(target), ops, before=BEFORE))
    )
    thread.start()
    try:
        deadline = time.monotonic() + 15.0
        while control.animating_state("@1") != "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert control.animating_state("@1") == "running"
        control.request_interrupt("@1")
    finally:
        thread.join(timeout=30.0)
    assert not thread.is_alive()
    return results[0]


@pytest.mark.parametrize(
    ("adopted", "users_paste"),
    [(False, "0"), (True, "0"), (True, "1")],
    ids=["dedicated", "adopted-nopaste", "adopted-paste"],
)
def test_an_interrupt_gives_the_users_paste_back(
    adopted: bool,
    users_paste: str,
    tmux_session: str,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    pane = vim_pane(tmux_session, tmp_path, wait_until, "")
    target = tmp_path / "t.py"
    target.write_text(BEFORE)
    shown = follower(pane, adopted=adopted)
    assert shown.show_fresh(str(target), BEFORE).outcome == "completed"
    # Set after show_fresh: its completion relock turns 'paste' off (not this
    # test's question; the relock always has).
    _keys(pane, f":set {'' if users_paste == '1' else 'no'}paste")
    paced = follower(pane, adopted=adopted, speed="lento")

    result = interrupted_edit(paced, target)

    assert result.outcome == "interrupted"
    modifiable, paste, *_ = state(pane, tmp_path, wait_until)
    assert (modifiable, paste) == ("1", users_paste)


def test_an_interrupt_hands_back_the_users_own_nomodifiable(
    tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    target = tmp_path / "t.py"
    target.write_text(BEFORE)
    pane = vim_pane(tmux_session, tmp_path, wait_until, f"edit {target}")
    _keys(pane, ":setlocal nomodifiable")
    follower(pane, adopted=True).ensure_showing(str(target))  # Claude's Read

    result = interrupted_edit(follower(pane, adopted=True, speed="lento"), target)

    assert result.outcome == "interrupted"
    assert state(pane, tmp_path, wait_until)[0] == "0"


def test_a_goto_that_fails_after_a_pause_gives_the_animated_buffer_its_readonly_back(
    tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    """Final M1 on a real Vim: an adopted animation is paused, and its
    post-pause goto does not land (here: the cursor is on another tab, as
    a user autocommand or a busy Vim leaves it). The animated buffer had
    the user's readonly, which the unlock took; it gets it back by handle,
    the buffer that IS current is not touched, and 'paste' is the user's."""
    target = tmp_path / "t.py"
    target.write_text(BEFORE)
    pane = vim_pane(tmux_session, tmp_path, wait_until, f"edit {target}")
    _keys(pane, ":setlocal readonly")
    follower(pane, adopted=True).ensure_showing(str(target))  # Claude's Read
    real_goto = TmuxVimFollower.goto_file
    gotos: list[str] = []

    def goto_that_fails_after_the_pause(self: TmuxVimFollower, file_path: str) -> None:
        gotos.append(file_path)
        if len(gotos) == 1:
            real_goto(self, file_path)
            return
        _keys(self.pane_id, ":tabnew")  # another, unnamed buffer is current
        raise NavigationFailed(f"the follower's Vim did not land on {file_path}")

    paced = follower(pane, adopted=True, speed="lento")
    target.write_text(LONG)
    errors: list[BaseException] = []

    def animate() -> None:
        try:
            paced.apply_edit(str(target), compute_edit_script(BEFORE, LONG), before=BEFORE)
        except NavigationFailed as error:
            errors.append(error)

    with patch.object(TmuxVimFollower, "goto_file", goto_that_fails_after_the_pause):
        thread = threading.Thread(target=animate)
        thread.start()
        try:
            assert wait_until(lambda: control.animating_state("@1") == "running", timeout=15.0)
            control.request_pause("@1")
            assert wait_until(lambda: control.animating_state("@1") == "paused", timeout=15.0)
            control.request_pause("@1")  # the P toggle: resume
        finally:
            thread.join(timeout=30.0)
    assert not thread.is_alive()
    assert len(errors) == 1
    # The remainder is on disk for the next P (animate._resume).
    assert control.has_pending_animation("@1")

    animated = f"bufnr('{target}')"
    expression = (
        f"getbufvar({animated}, '&readonly') . getbufvar({animated}, 'vaf_ro_ours', 'x')"
        f" . has_key(getbufvar({animated}, ''), 'vaf_user_ro') . &readonly"
        " . has_key(b:, 'vaf_ro_ours')"
    )
    _modifiable, paste, answer, *_ = state(pane, tmp_path, wait_until, expression)
    assert (answer, paste) == ("10000", "0")
