"""The tmux follower's Vim-script steps against a hostile or unusual user Vim,
on a real tmux+vim at the follower pane's real 49 columns.

Every Vim here is hermetic (`-u NONE -i NONE`) and the user's setup is typed
into it; every state is read back with writefile(), never capture-pane, and
the read is sent with NO Escape before it, so a prompt the follower left
behind eats it and the test says so.

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
from collections.abc import Callable
from pathlib import Path

import pytest

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


def follower(pane: str, *, adopted: bool) -> TmuxVimFollower:
    FollowerState.set("@1", "tmux", pane, adopted=adopted, speed="instant")
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
