"""Real-nvim twin of tests/test_integration_adopted_close.py: closing a
follower tab in an ADOPTED nvim (the eviction past max_tabs, and
`claude-follow stop`) never discards the user's work.

Final review of backlog-sweep-3 (C1), reproduced at acce2b2: the user's own
buffer, put into `open_files` by a Read, was wiped by `bwipeout!` five Reads
later, unsaved text and all. "Modified" here is nvim's clean test
(_IS_CLEAN_LUA), because this backend never writes its buffers: a buffer the
follower animated to completion is 'modified' yet holds only Claude's text.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import commands, hooks  # noqa: E402
from vim_ai_follower.backends.nvim import _EVICT_LUA, NvimFollower  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402

pytestmark = pytest.mark.integration

UNSAVED = "UNSAVED WORK"
_WINDOW = "term-c1"  # session._standalone_id({"TERM_SESSION_ID": "c1"})


def _adopted(headless_nvim: str) -> tuple[Any, NvimFollower]:
    FollowerState.set(_WINDOW, backend="nvim", target=headless_nvim, adopted=True, speed="instant")
    nvim = pynvim.attach("socket", path=headless_nvim)
    return nvim, NvimFollower(socket_path=headless_nvim, window_id=_WINDOW, pace_seconds=0.0)


def _read(follower: NvimFollower, path: Path, maximum_tabs: int) -> None:
    """The calls _handle_hook_post_read makes for a Read."""
    hooks._ensure_buffer(_WINDOW, follower, str(path))
    current = FollowerState.read(_WINDOW)
    assert current is not None
    hooks._touch_and_evict(_WINDOW, follower, current, str(path), maximum_tabs)


def _buffer(nvim: Any, path: Path) -> Any | None:
    for buffer in nvim.buffers:
        if buffer.name and os.path.realpath(buffer.name) == str(path):
            return buffer
    return None


def _others(root: Path, count: int) -> list[Path]:
    others = []
    for index in range(count):
        other = root / f"o{index}.py"
        other.write_text(f"o = {index}\n")
        others.append(other)
    return others


def test_reads_never_wipe_the_users_own_modified_buffer(
    headless_nvim: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = Path(os.path.realpath(tmp_path))
    mine = root / "mine.py"
    mine.write_text("mine = 1\n")
    others = _others(root, 5)
    nvim, follower = _adopted(headless_nvim)
    nvim.command(f"edit {mine}")
    nvim.current.buffer.append(UNSAVED)

    caplog.set_level(logging.INFO, logger="vim_ai_follower")
    for path in [mine, *others]:
        _read(follower, path, 5)

    buffer = _buffer(nvim, mine)
    assert buffer is not None
    assert buffer[:] == ["mine = 1", UNSAVED]
    current = FollowerState.read(_WINDOW)
    assert current is not None
    assert str(mine) not in current.open_files
    assert any(str(mine) in record.getMessage() for record in caplog.records)


def test_reads_never_wipe_the_users_own_buffer_even_unmodified(
    headless_nvim: str, tmp_path: Path
) -> None:
    root = Path(os.path.realpath(tmp_path))
    mine = root / "mine.py"
    mine.write_text("mine = 1\n")
    others = _others(root, 1)
    nvim, follower = _adopted(headless_nvim)
    nvim.command(f"edit {mine}")

    for path in [mine, *others]:
        _read(follower, path, 1)

    buffer = _buffer(nvim, mine)
    assert buffer is not None
    assert buffer[:] == ["mine = 1"]


def test_a_tab_the_follower_opened_for_the_users_buffer_is_closed_not_wiped(
    headless_nvim: str, tmp_path: Path
) -> None:
    """two.py is loaded but in no window ('hidden', nvim's default); a Read
    opens a tab for it. Evicting it closes that tab only."""
    root = Path(os.path.realpath(tmp_path))
    mine = root / "mine.py"
    mine.write_text("mine = 1\n")
    two = root / "two.py"
    two.write_text("two = 2\n")
    others = _others(root, 1)
    nvim, follower = _adopted(headless_nvim)
    nvim.command(f"edit {two}")
    nvim.command(f"edit {mine}")

    _read(follower, two, 1)
    assert len(nvim.api.list_tabpages()) == 2
    _read(follower, others[0], 1)

    buffer = _buffer(nvim, two)
    assert buffer is not None
    assert buffer[:] == ["two = 2"]
    # mine.py's tab and o0.py's: two.py's tab is gone.
    assert len(nvim.api.list_tabpages()) == 2
    assert all(nvim.api.win_get_buf(window).number != buffer.number for window in nvim.windows)


def test_a_follower_tab_the_user_typed_into_survives_eviction(
    headless_nvim: str, tmp_path: Path
) -> None:
    root = Path(os.path.realpath(tmp_path))
    first = root / "x.py"
    first.write_text("x = 1\n")
    second = root / "y.py"
    second.write_text("y = 1\n")
    nvim, follower = _adopted(headless_nvim)

    _read(follower, first, 1)
    nvim.current.buffer.append(UNSAVED)
    _read(follower, second, 1)

    buffer = _buffer(nvim, first)
    assert buffer is not None
    assert buffer[:] == ["x = 1", UNSAVED]


def test_a_follower_tab_nobody_typed_into_is_still_evicted(
    headless_nvim: str, tmp_path: Path
) -> None:
    """max_tabs still caps the follower's own tabs: a Read's tab, and a tab
    an animation completed in (modified, but only Claude's text)."""
    root = Path(os.path.realpath(tmp_path))
    first = root / "x.py"
    first.write_text("x = 1\n")
    animated = root / "w.py"
    animated.write_text("w = 1\n")
    second = root / "y.py"
    second.write_text("y = 1\n")
    nvim, follower = _adopted(headless_nvim)

    _read(follower, first, 1)
    assert follower.show_fresh(str(animated), "w = 1\n", in_new_tab=True).outcome == "completed"
    current = FollowerState.read(_WINDOW)
    assert current is not None
    hooks._touch_and_evict(_WINDOW, follower, current, str(animated), 1)
    assert _buffer(nvim, first) is None
    _read(follower, second, 1)
    assert _buffer(nvim, animated) is None
    assert _buffer(nvim, second) is not None


def test_stop_keeps_the_users_buffers_and_closes_the_followers_own(
    headless_nvim: str, tmp_path: Path
) -> None:
    root = Path(os.path.realpath(tmp_path))
    mine = root / "mine.py"
    mine.write_text("mine = 1\n")
    first = root / "x.py"
    first.write_text("x = 1\n")
    nvim, follower = _adopted(headless_nvim)
    nvim.command(f"edit {mine}")
    nvim.current.buffer.append(UNSAVED)
    _read(follower, mine, 5)
    _read(follower, first, 5)

    assert commands.cmd_stop({"TERM_SESSION_ID": "c1"}) == 0

    buffer = _buffer(nvim, mine)
    assert buffer is not None
    assert buffer[:] == ["mine = 1", UNSAVED]
    assert _buffer(nvim, first) is None
    assert FollowerState.read(_WINDOW) is None


def test_a_stop_racing_a_read_never_wipes_the_users_modified_buffer(
    headless_nvim: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Review of sweep-4 T4+T5, C1: `claude-follow stop` lands while a Read
    navigates, so the state the hook read is gone when its eviction runs.
    close_tab re-reads it, and an unknown state used to mean "dedicated":
    goto_file + bwipeout! of the user's unsaved buffer."""
    root = Path(os.path.realpath(tmp_path))
    mine = root / "mine.py"
    mine.write_text("mine = 1\n")
    other = root / "other.py"
    other.write_text("o = 1\n")
    nvim, follower = _adopted(headless_nvim)
    nvim.command(f"edit {mine}")
    nvim.current.buffer.append(UNSAVED)
    FollowerState.update(_WINDOW, open_files=(str(mine),))

    caplog.set_level(logging.INFO, logger="vim_ai_follower")
    hooks._ensure_buffer(_WINDOW, follower, str(other))
    current = FollowerState.read(_WINDOW)
    assert current is not None
    FollowerState.clear(_WINDOW)  # the racing stop
    hooks._touch_and_evict(_WINDOW, follower, current, str(other), 1)

    buffer = _buffer(nvim, mine)
    assert buffer is not None, "the user's buffer was wiped"
    assert buffer[:] == ["mine = 1", UNSAVED]
    assert any("follower state is gone" in record.getMessage() for record in caplog.records)


def test_a_stop_then_start_racing_a_read_never_wipes_the_users_modified_buffer(
    headless_nvim: str, tmp_path: Path
) -> None:
    """Re-review of sweep-4 T4+T5, N1: a `stop` and then a `start` of a
    DEDICATED follower land while a Read navigates in the adopted nvim. The
    new state says dedicated, but for another socket: it is not this
    follower's, so the eviction must not wipe."""
    root = Path(os.path.realpath(tmp_path))
    mine = root / "mine.py"
    mine.write_text("mine = 1\n")
    other = root / "other.py"
    other.write_text("o = 1\n")
    nvim, follower = _adopted(headless_nvim)
    nvim.command(f"edit {mine}")
    nvim.current.buffer.append(UNSAVED)
    FollowerState.update(_WINDOW, open_files=(str(mine),))

    hooks._ensure_buffer(_WINDOW, follower, str(other))
    current = FollowerState.read(_WINDOW)
    assert current is not None
    FollowerState.clear(_WINDOW)  # the racing stop
    FollowerState.set(_WINDOW, "nvim", "/elsewhere/new.sock", adopted=False)  # and start
    hooks._touch_and_evict(_WINDOW, follower, current, str(other), 1)

    buffer = _buffer(nvim, mine)
    assert buffer is not None, "the user's buffer was wiped"
    assert buffer[:] == ["mine = 1", UNSAVED]


def test_reads_leave_no_empty_buffer_behind(headless_nvim: str, tmp_path: Path) -> None:
    """Final review of backlog-sweep-3, M7: every Read through
    _open_from_disk left the empty buffer its `:tabnew` made, hidden
    (buffers 3, 5, 7, … = '')."""
    root = Path(os.path.realpath(tmp_path))
    nvim, follower = _adopted(headless_nvim)

    for path in _others(root, 5):
        _read(follower, path, 5)

    hidden_empty = [
        buffer.number
        for buffer in nvim.buffers
        if buffer.name == "" and not nvim.funcs.win_findbuf(buffer.number)
    ]
    assert hidden_empty == []


def test_a_wipe_the_editor_refuses_is_not_reported_as_done(headless_nvim: str) -> None:
    """Review m1 of T24c: in the command-line window `bwipeout!` fails with
    E11 behind `silent!`, and _EVICT_LUA still answered "wiped"."""
    nvim, _follower = _adopted(headless_nvim)
    nvim.command("tabnew")
    buffer = nvim.api.create_buf(True, False)
    nvim.api.buf_set_lines(buffer, 0, -1, True, ["claude"])
    nvim.api.buf_set_option(buffer, "modified", False)
    nvim.api.buf_set_var(buffer, "vaf_created", 1)
    nvim.api.win_set_buf(0, buffer)
    nvim.command("tabfirst")
    nvim.input("q:")
    deadline = time.monotonic() + 5.0
    while nvim.call("getcmdwintype") != ":" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert nvim.call("getcmdwintype") == ":"

    assert nvim.exec_lua(_EVICT_LUA, buffer.handle, False) == "not wiped"
    assert nvim.api.buf_is_valid(buffer)
    nvim.input("<C-c>")
    nvim.close()
