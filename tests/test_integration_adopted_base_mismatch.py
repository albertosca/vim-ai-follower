"""The base-mismatch guard in an ADOPTED tmux Vim, against a real tmux+vim.

The tmux twin of tests/test_nvim_integration_adopted_base_mismatch.py (the
policy is described there). The follower is started dedicated and then
flagged adopted in FollowerState, which is the one thing the hooks read to
tell the two apart; the Vim itself is then driven the way a user would drive
their own editor.

The buffer is read back with writefile(), never capture-pane (which shows the
rendered screen, not the buffer).
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

from vim_ai_follower.hooks import BASE_DIFFERS_CUE
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.integration

USER_LINE = "USER_UNSAVED = 1"


def _keys(pane: str, *commands: str) -> None:
    for command in commands:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", command], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)


def _buffer_lines(
    pane: str, target: Path, tmp_path: Path, wait_until: Callable[..., bool]
) -> list[str]:
    """The real buffer of `target`, dumped by Vim behind a sentinel line."""
    dump = tmp_path / "dump.txt"
    dump.unlink(missing_ok=True)
    _keys(
        pane,
        f":call writefile(['DUMP'] + getbufline(bufnr('{target}'), 1, '$'), '{dump}')",
    )
    assert wait_until(lambda: dump.exists() and dump.read_text().startswith("DUMP\n"), timeout=5.0)
    return dump.read_text().splitlines()[1:]


def _adopt(window_id: str) -> None:
    FollowerState.update(window_id, adopted=True)


def test_a_file_the_user_opened_in_their_vim_is_not_wiped_by_a_fresh_write(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """Pre-dates B2: show_fresh's by-number `bwipeout!` took the user's own
    loaded buffer, unsaved line and all."""
    log, pane, window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    _write(monkeypatch, log, wait_until, tmp_path / "seed.py", "seed = 1\n")
    _adopt(window_id)
    target = tmp_path / "b.py"
    target.write_text("a = 1\n")
    _keys(pane, f":tabedit {target}", f":call append(0, '{USER_LINE}')")
    assert wait_until(
        lambda: _buffer_lines(pane, target, tmp_path, wait_until) == [USER_LINE, "a = 1"],
        timeout=5.0,
    )

    _hook(monkeypatch, "pre", "Write", target)
    target.write_text("a = 2\n")
    _hook(monkeypatch, "post", "Write", target)

    assert _buffer_lines(pane, target, tmp_path, wait_until) == [USER_LINE, "a = 1"]
    assert TmuxPane(pane_id=pane).title() == BASE_DIFFERS_CUE
    assert str(target) in (tmp_path / "hook.log").read_text()


def test_the_users_line_survives_and_after_e_bang_the_next_edit_animates(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    log, pane, window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    _write(monkeypatch, log, wait_until, target, "x = 1\ny = 2\n")
    _adopt(window_id)
    # The user unlocks the follower's buffer and types into it, unsaved.
    _keys(pane, ":setlocal modifiable noreadonly", f":call append(0, '{USER_LINE}')")
    kept = [USER_LINE, "x = 1", "y = 2"]
    assert wait_until(lambda: _buffer_lines(pane, target, tmp_path, wait_until) == kept, 5.0)

    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text("x = 1\ny = 3\n")
    _hook(monkeypatch, "post", "Edit", target)

    assert _buffer_lines(pane, target, tmp_path, wait_until) == kept
    assert TmuxPane(pane_id=pane).title() == BASE_DIFFERS_CUE
    state = FollowerState.read(window_id)
    assert state is not None
    assert str(target) in state.stale_files

    # The user takes Claude's version; the next edit is a diff onto it.
    _keys(pane, ":e!")
    assert wait_until(
        lambda: _buffer_lines(pane, target, tmp_path, wait_until) == ["x = 1", "y = 3"], 5.0
    )
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text("x = 1\ny = 3\nz = 4\n")
    _hook(monkeypatch, "post", "Edit", target)

    after = ["x = 1", "y = 3", "z = 4"]
    assert wait_until(lambda: _last_state(log, target.name) == after, timeout=15.0)
    states = _states(log, target.name)
    # A diff keeps the first two lines on screen throughout; a retype would
    # have wiped them first.
    assert all(s[:2] == ["x = 1", "y = 3"] for s in states), states
    state = FollowerState.read(window_id)
    assert state is not None
    assert str(target) not in state.stale_files
    assert TmuxPane(pane_id=pane).title() != BASE_DIFFERS_CUE
