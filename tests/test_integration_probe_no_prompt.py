"""The tmux probe must never leave a blocking prompt in the follower's Vim.

probe_buffer asks Vim to writefile() the buffer into a probe file. When that
write fails — an unwritable cache directory is the everyday case — Vim prints
`E482: Can't create file <path>`, and at the follower pane's real width of 49
columns the long path wraps the message into a "Press ENTER or type command to
continue" prompt (measured 2026-09-25 on a real Vim, before the guard). In an
adopted Vim nothing is sent after the probe, so the prompt stayed there for
the user on every edit.

Rule: on any failure the probe leaves no prompt and answers "unknown" within
its timeout. These run against a real tmux+vim at the real width: the
conftest window is 100 columns, and the follower's `split-window -h` makes it
49 — asserted, not assumed.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from test_integration_edit_no_reload import (
    ONE_DEF,
    THREE_DEFS,
    _hook,
    _last_state,
    _observed_follower,
    _write,
)

from vim_ai_follower.backends import tmux_vim
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.hooks import PROBE_UNKNOWN_CUE
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.integration


@pytest.fixture
def unwritable_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the probe file into a read-only directory, so Vim's writefile()
    fails with E482. Python's own pre-probe unlink of a missing file still
    succeeds there, so it is really Vim's write that fails."""
    locked = tmp_path / "locked"
    locked.mkdir()
    monkeypatch.setattr(
        tmux_vim, "_probe_path", lambda pane_id: locked / f"probe-{pane_id.lstrip('%')}.txt"
    )
    monkeypatch.setattr(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 1.0)
    locked.chmod(0o500)
    try:
        yield locked
    finally:
        locked.chmod(0o700)


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


def _assert_no_prompt(pane: str) -> None:
    screen = _screen(pane)
    assert "Press ENTER" not in screen, screen
    assert "E482" not in screen, screen


def _still_takes_commands(pane: str, tmp_path: Path, wait_until: Callable[..., bool]) -> None:
    """A plain keystroke-driven command lands and runs."""
    alive = tmp_path / "alive.txt"
    for keys in ([":call writefile(['alive'], '" + str(alive) + "')"], ["Enter"]):
        literal = keys[0] != "Enter"
        subprocess.run(
            ["tmux", "send-keys", "-t", pane, *(["-l", "--"] if literal else []), *keys],
            check=True,
        )
    assert wait_until(lambda: alive.exists(), timeout=5.0), _screen(pane)


def test_an_unwritable_probe_answers_unknown_and_leaves_no_prompt(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    unwritable_probe: Path,
) -> None:
    log, pane, _ = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    assert _width(pane) == 49
    target = tmp_path / "a.py"
    _write(monkeypatch, log, wait_until, target, ONE_DEF)

    answer = TmuxVimFollower(pane_id=pane).probe_buffer(str(target), ONE_DEF)

    assert answer == "unknown"
    _assert_no_prompt(pane)
    _still_takes_commands(pane, tmp_path, wait_until)


def test_an_adopted_vim_gets_the_honest_cue_and_no_prompt(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    unwritable_probe: Path,
) -> None:
    """Adopted: the unknown answer leaves the buffer alone and sends nothing
    afterwards — so a prompt from the probe would sit in the user's Vim."""
    log, pane, window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    assert _width(pane) == 49
    target = tmp_path / "a.py"
    _write(monkeypatch, log, wait_until, target, ONE_DEF)
    FollowerState.update(window_id, adopted=True)

    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text(THREE_DEFS)
    _hook(monkeypatch, "post", "Edit", target)

    assert TmuxPane(pane_id=pane).title() == PROBE_UNKNOWN_CUE
    assert _last_state(log, target.name) == ONE_DEF.splitlines()  # left alone
    _assert_no_prompt(pane)
    _still_takes_commands(pane, tmp_path, wait_until)


def test_a_dedicated_follower_retypes_when_the_probe_cannot_answer(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    unwritable_probe: Path,
) -> None:
    log, pane, _ = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    assert _width(pane) == 49
    target = tmp_path / "a.py"
    _write(monkeypatch, log, wait_until, target, ONE_DEF)

    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text(THREE_DEFS)
    _hook(monkeypatch, "post", "Edit", target)

    assert wait_until(lambda: _last_state(log, target.name) == THREE_DEFS.splitlines(), 15.0)
    _assert_no_prompt(pane)
