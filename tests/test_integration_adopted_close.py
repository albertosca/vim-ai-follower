"""Real tmux+vim proof that closing a follower tab in an ADOPTED Vim never
discards the user's work: neither the eviction past max_tabs nor
`claude-follow stop`.

Final review of backlog-sweep-3 (C1, 2026-10-05), reproduced at acce2b2: in
an adopted Vim, Claude Reading the file the user is editing put the user's
own buffer into `open_files`, and five more Reads evicted it with
`bwipeout!`, unsaved text and all; `stop` wiped every tracked buffer the same
way. Now a modified buffer is never wiped, and a buffer the follower did not
create is never wiped at all: the follower closes only a tab it opened for
it, else just forgets it. A buffer the follower created and nobody typed
into is still evicted, so max_tabs keeps capping the follower's own tabs.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_same_file import _keys, _tmux, _user_vim

from vim_ai_follower import commands, hooks
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

UNSAVED = "UNSAVED WORK"


def _buffer(
    pane: str, out: Path, name: str, wait_until: Callable[..., bool]
) -> dict[str, str | list[str]]:
    """Whether a buffer named `name` exists, is loaded and modified, its
    lines, and the tab count — straight out of Vim."""
    out.unlink(missing_ok=True)
    _keys(pane, "Escape", "Escape")
    _keys(
        pane,
        "-l",
        "--",
        f":let g:n = bufnr('^{name}$') | call writefile(['EXISTS=' . (g:n > 0),"
        " 'LOADED=' . bufloaded(g:n), 'MODIFIED=' . getbufvar(g:n, '&modified', 0),"
        " 'MA=' . getbufvar(g:n, '&modifiable', 0), 'TABS=' . tabpagenr('$')]"
        f" + getbufline(g:n, 1, '$') + ['END'], '{out}')",
    )
    _keys(pane, "Enter")

    def answered() -> bool:
        return out.exists() and out.read_text().endswith("END\n")

    assert wait_until(answered, timeout=10.0), _tmux("capture-pane", "-p", "-t", pane)
    lines = out.read_text().splitlines()[:-1]
    fields: dict[str, str | list[str]] = {}
    for line in lines[:5]:
        key, value = line.split("=", 1)
        fields[key] = value
    fields["LINES"] = lines[5:]
    return fields


def _adopted(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, root: Path, name: str
) -> tuple[str, str, TmuxVimFollower]:
    pane = _user_vim(tmux_session, monkeypatch, root, name)
    window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
    FollowerState.set(
        window_id, backend="tmux", target=pane, adopted=True, speed="instant", shown_any=True
    )
    return pane, window_id, TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id=window_id)


def _read(window_id: str, follower: TmuxVimFollower, path: Path, max_tabs: int) -> None:
    """The calls _handle_hook_post_read makes for a Read."""
    hooks._ensure_buffer(window_id, follower, str(path))
    current = FollowerState.read(window_id)
    assert current is not None
    hooks._touch_and_evict(window_id, follower, current, str(path), max_tabs)


def _type_unsaved(pane: str) -> None:
    _keys(pane, "Escape", "Escape")
    _keys(pane, "-l", ":setlocal modifiable noreadonly")
    _keys(pane, "Enter")
    _keys(pane, "-l", "Go" + UNSAVED)
    _keys(pane, "Escape")
    time.sleep(0.3)


def _others(root: Path, count: int) -> list[Path]:
    others = []
    for index in range(count):
        other = root / f"o{index}.py"
        other.write_text(f"o = {index}\n")
        others.append(other)
    return others


def test_reads_never_wipe_the_users_own_modified_buffer(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reviewer's reproduction: the user edits mine.py, unsaved; Claude
    Reads it, then five other files (max_tabs 5)."""
    root = Path(os.path.realpath(tmp_path))
    mine = root / "mine.py"
    mine.write_text("mine = 1\n")
    others = _others(root, 5)
    pane, window_id, follower = _adopted(tmux_session, monkeypatch, root, "mine.py")
    _keys(pane, "-l", "Go" + UNSAVED)
    _keys(pane, "Escape")

    caplog.set_level(logging.INFO, logger="vim_ai_follower")
    for path in [mine, *others]:
        _read(window_id, follower, path, 5)

    report = _buffer(pane, tmp_path / "b.txt", "mine.py", wait_until)
    assert report["EXISTS"] == "1", report
    assert report["LINES"] == ["mine = 1", UNSAVED], report
    assert mine.read_text() == "mine = 1\n"
    current = FollowerState.read(window_id)
    assert current is not None
    assert str(mine) not in current.open_files  # forgotten, not tracked
    assert any(str(mine) in record.getMessage() for record in caplog.records)


def test_reads_never_wipe_the_users_own_buffer_even_unmodified(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """A buffer the user opened is theirs, saved or not: eviction forgets it
    and leaves it where it is (and gives back the follower's Read lock)."""
    root = Path(os.path.realpath(tmp_path))
    mine = root / "mine.py"
    mine.write_text("mine = 1\n")
    others = _others(root, 1)
    pane, window_id, follower = _adopted(tmux_session, monkeypatch, root, "mine.py")

    for path in [mine, *others]:
        _read(window_id, follower, path, 1)

    report = _buffer(pane, tmp_path / "b.txt", "mine.py", wait_until)
    assert report["EXISTS"] == "1", report
    assert report["MA"] == "1", report  # the Read's lock is not left behind


def test_a_tab_the_follower_opened_for_the_users_buffer_is_closed_not_wiped(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """`vim mine.py two.py` leaves two.py listed and unloaded; a Read of it
    opens a tab (`:tab sbuffer`). Evicting it closes that tab only."""
    root = Path(os.path.realpath(tmp_path))
    (root / "mine.py").write_text("mine = 1\n")
    two = root / "two.py"
    two.write_text("two = 2\n")
    others = _others(root, 1)
    pane, window_id, follower = _adopted(tmux_session, monkeypatch, root, "mine.py two.py")

    _read(window_id, follower, two, 1)
    assert _buffer(pane, tmp_path / "b0.txt", "two.py", wait_until)["TABS"] == "2"
    _read(window_id, follower, others[0], 1)

    report = _buffer(pane, tmp_path / "b1.txt", "two.py", wait_until)
    assert report["EXISTS"] == "1", report
    # mine.py's tab and o0.py's: two.py's tab is gone.
    assert report["TABS"] == "2", report


def test_a_follower_tab_the_user_typed_into_survives_eviction(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The narrower form: the follower opened x.py, the user unlocked it and
    typed; the next Read (max_tabs 1) must not wipe it."""
    root = Path(os.path.realpath(tmp_path))
    (root / "user.txt").write_text("user line\n")
    first = root / "x.py"
    first.write_text("x = 1\n")
    second = root / "y.py"
    second.write_text("y = 1\n")
    pane, window_id, follower = _adopted(tmux_session, monkeypatch, root, "user.txt")

    _read(window_id, follower, first, 1)
    _type_unsaved(pane)
    _read(window_id, follower, second, 1)

    report = _buffer(pane, tmp_path / "b.txt", "x.py", wait_until)
    assert report["EXISTS"] == "1", report
    assert report["LINES"] == ["x = 1", UNSAVED], report


def test_a_follower_tab_nobody_typed_into_is_still_evicted(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """max_tabs still caps the follower's own tabs in an adopted Vim."""
    root = Path(os.path.realpath(tmp_path))
    (root / "user.txt").write_text("user line\n")
    first = root / "x.py"
    first.write_text("x = 1\n")
    second = root / "y.py"
    second.write_text("y = 1\n")
    pane, window_id, follower = _adopted(tmux_session, monkeypatch, root, "user.txt")

    _read(window_id, follower, first, 1)
    _read(window_id, follower, second, 1)

    report = _buffer(pane, tmp_path / "b.txt", "x.py", wait_until)
    assert report["EXISTS"] == "0", report
    assert report["TABS"] == "2", report  # user.txt's and y.py's


def test_stop_keeps_the_users_buffers_and_closes_the_followers_own(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """`claude-follow stop` in an adopted Vim: the user's modified buffer (a
    Read put it in open_files) survives; the follower's clean tab goes."""
    root = Path(os.path.realpath(tmp_path))
    mine = root / "mine.py"
    mine.write_text("mine = 1\n")
    first = root / "x.py"
    first.write_text("x = 1\n")
    pane, window_id, follower = _adopted(tmux_session, monkeypatch, root, "mine.py")
    _keys(pane, "-l", "Go" + UNSAVED)
    _keys(pane, "Escape")
    _read(window_id, follower, mine, 5)
    _read(window_id, follower, first, 5)

    assert commands.cmd_stop({"TMUX_PANE": pane}) == 0

    report = _buffer(pane, tmp_path / "b0.txt", "mine.py", wait_until)
    assert report["EXISTS"] == "1", report
    assert report["LINES"] == ["mine = 1", UNSAVED], report
    assert _buffer(pane, tmp_path / "b1.txt", "x.py", wait_until)["EXISTS"] == "0"
    assert FollowerState.read(window_id) is None
