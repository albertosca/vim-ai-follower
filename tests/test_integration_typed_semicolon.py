"""Typed text that ends in `;` reaches a REAL tmux-backend Vim byte-exact.

tmux reads any send-keys argument that ends in `;` as a command separator
and drops the `;` (a `\\;` ending keeps a literal `;`), so `int a = 1;` was
typed as `int a = 1` and a lone `;` as nothing. In a dedicated Vim an
interrupted Write followed by the user's `:w` then saved the damaged
partial, and the hand-off released Claude with it (review C1 of T23)."""

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
from test_integration_adopted_close import _adopted
from test_integration_edit_no_reload import _MARK, _OBSERVER, _observed_follower, _states
from test_integration_handoff_write import _keys as _command_keys
from test_integration_handoff_write import _lines, _prove_private
from test_integration_same_file import _keys

from vim_ai_follower import cli, control
from vim_ai_follower.diff import compute_edit_script
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.integration

ENDINGS = [
    "int a = 1;",
    "b = 2\\;",
    "c = 3;;",
    ";",
    "d = 4 ;",
    "e = ab;\\;",
    "if (x) {",
    "}",
    "f = ~",
    "g = '#{pane_id}'",
    "h = 5 \\",
    "-i = 6",
    "plain_line = 7",
    "k = 8;",
    "m = 9;",
]
CONTENT = "".join(line + "\n" for line in ENDINGS)

HOSTILE = [
    "a;",
    "b\\;",
    "c;;",
    ";",
    "x \\;",
    "ab;\\;",
    ";;;",
    "m;id line; more",
    "é",
    "漢",
    "😀",
    "tab\there",
    "trail\\",
    "\\",
    "-lead",
    "#{pane_id}",
    "~",
    "{",
    "}",
    "",
]


def _is_typed_prefix(state: list[str], intended: list[str]) -> bool:
    """`state` is what typing `intended` top-down can show at some moment:
    every line but the last whole, the last one possibly half-typed."""
    if not state or len(state) > len(intended):
        return False
    *whole, last = state
    return whole == intended[: len(whole)] and intended[len(whole)].startswith(last)


def test_send_text_types_every_hostile_string_byte_exact(
    tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    _prove_private()
    vim = "vim -u NONE -N -n -c 'set paste encoding=utf-8'"
    pane = subprocess.run(
        ["tmux", "split-window", "-h", "-t", tmux_session, "-P", "-F", "#{pane_id}", vim],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    time.sleep(0.5)
    sender = TmuxPane(pane)
    typed: dict[str, str] = {}
    for index, text in enumerate(HOSTILE):
        out = tmp_path / f"typed-{index}.txt"
        _keys(pane, "Escape", "Escape")
        _keys(pane, "-l", "--", ":enew!\r")
        _keys(pane, "-l", "--", "i")
        sender.send_text(text)
        _keys(pane, "Escape")
        _keys(pane, "-l", "--", f":call writefile(getline(1, '$') + ['END'], '{out}', 'b')\r")
        assert wait_until(
            lambda out=out: out.exists() and out.read_bytes().endswith(b"\nEND"), timeout=10.0
        )
        typed[text] = out.read_bytes().decode()[: -len("\nEND")]
    wrong = {text: got for text, got in typed.items() if got != text}
    assert wrong == {}


def test_an_interrupted_write_saves_the_partial_byte_for_byte(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    _prove_private()
    log, pane, window_id = _observed_follower(
        tmux_session, monkeypatch, tmp_path, wait_until, "--speed", "lento"
    )
    target = tmp_path / "new.c"
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    body = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    assert cli.main(["hook", "pre"]) == 0
    target.write_text(CONTENT)
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    thread = threading.Thread(target=lambda: cli.main(["hook", "post"]))
    thread.start()
    full = CONTENT.splitlines()
    try:
        assert wait_until(lambda: control.animating_state(window_id) == "running", timeout=10.0)
        assert wait_until(
            lambda: any(12 <= len(state) < len(full) for state in _states(log, target.name)),
            timeout=30.0,
        )
        control.request_interrupt(window_id)
        assert wait_until(
            lambda: (
                control.animating_state(window_id) == "handoff"
                and control.has_pending_animation(window_id)
            ),
            timeout=15.0,
        )
        pending = control.load_pending_animation(window_id)
        assert pending is not None and pending.partial is not None
        partial = pending.partial.splitlines()
        assert partial == full[: len(partial)]
        _command_keys(pane, ":w")
        assert wait_until(lambda: _lines(target) != full, timeout=10.0)
        time.sleep(0.5)
        saved = target.read_bytes()
        thread.join(timeout=15.0)
        assert saved == pending.partial.encode()
    finally:
        if thread.is_alive():
            target.write_text("released by the test\n")
            thread.join(timeout=15.0)


def test_an_adopted_vim_never_shows_a_line_without_its_semicolon(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    root = Path(os.path.realpath(tmp_path))
    mine = root / "mine.c"
    base = "top = 0\n"
    mine.write_text(base)
    pane, window_id, _instant = _adopted(tmux_session, monkeypatch, root, "mine.c")
    FollowerState.set(
        window_id, backend="tmux", target=pane, adopted=True, speed="normal", shown_any=True
    )
    from vim_ai_follower.backends.tmux_vim import TmuxVimFollower

    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.02, window_id=window_id)
    log = root / "observer.log"
    observer = root / "observer.vim"
    observer.write_text(f"let g:vaf_obs_log = '{log}'\n{_OBSERVER}")
    _keys(pane, "-l", "--", f":source {observer}\r")
    assert wait_until(log.exists, timeout=10.0)
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    after = base + CONTENT
    mine.write_text(after)
    assert control.try_acquire_animating(window_id)
    try:
        result = follower.apply_edit(str(mine), compute_edit_script(base, after), base)
    finally:
        control.clear_animating(window_id)
    assert result.outcome == "completed"
    intended = after.splitlines()
    assert wait_until(lambda: _states(log, "mine.c")[-1:] == [intended], timeout=10.0)
    states = _states(log, "mine.c")
    assert len(states) > 3, "the observer saw no middle of the animation"
    torn = [state for state in states if not _is_typed_prefix(state, intended)]
    assert torn == []
