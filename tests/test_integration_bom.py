"""Real tmux+vim proof that a UTF-8 file with a BOM is followed like any
other: Vim loads the leading U+FEFF into 'bomb' and leaves it out of the
buffer, while the hook's snapshot keeps it.

Final review of backlog-sweep-3 (I2), reproduced at acce2b2: the base probe
compared the buffer with the snapshot verbatim and said "differs" for every
BOM file, every time. A dedicated follower wiped and retyped the whole file
on each Edit, typing the BOM character into line 1; an adopted one never
animated it and showed a cue `:e!` could not clear.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_edit_no_reload import _MARK, _hook, _last_state, _observed_follower, _states
from test_integration_same_file import _user_vim

from vim_ai_follower.backends.tmux_vim import TmuxVimFollower

pytestmark = pytest.mark.integration

BOM = "﻿"
ONE = BOM + "x = 1\ny = 2\n"
TWO = BOM + "x = 1\ny = 2\nz = 3\n"


def test_an_edit_of_a_bom_file_is_typed_as_a_diff_without_the_bom(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    log, follower_pane, _ = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    _hook(monkeypatch, "pre", "Write", target)
    target.write_text(ONE)
    _hook(monkeypatch, "post", "Write", target)
    assert wait_until(lambda: _last_state(log, "a.py") == ["x = 1", "y = 2"], timeout=15.0)
    written = [json.loads(raw)["lines"] for raw in log.read_text().splitlines()]
    assert not any(BOM in line for state in written for line in state), written

    follower = TmuxVimFollower(pane_id=follower_pane)
    assert follower.probe_buffer(str(target), ONE) == "holds"

    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text(TWO)
    _hook(monkeypatch, "post", "Edit", target)
    after = ["x = 1", "y = 2", "z = 3"]
    assert wait_until(lambda: _last_state(log, "a.py") == after, timeout=15.0)
    states = _states(log, "a.py")
    # A diff onto the kept lines, never a wipe-and-retype (which starts from
    # one empty line) and never a typed BOM.
    assert all(state[:2] == ["x = 1", "y = 2"] for state in states), states
    assert not any(BOM in line for state in states for line in state), states
    assert target.read_text() == TWO


def test_an_adopted_vim_on_a_bom_file_holds_its_base(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = Path(os.path.realpath(tmp_path))
    target = root / "a.py"
    target.write_text(ONE)
    pane = _user_vim(tmux_session, monkeypatch, root, "a.py")
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0)
    assert follower.probe_buffer(str(target), ONE) == "holds"
