"""Real tmux+vim proof for show_fresh's `:file {path}` line, flagged by the
Edit-reload fix review (2026-09-23) as sent raw: `#`, `%` and a space are
each live on Vim's command line the way `:tab drop`'s argument was before
`fnameescape()` was added there (see test_integration_goto_file_escaping.py)
— `#`/`%` expand to the alternate/current file and a space splits the
`:file` argument into two, so the buffer can be renamed onto the wrong path.

The reproduction is end to end through the real hook pipeline, because the
symptom is not the rename alone: `show_fresh` (a Write, since the file is
fresh) sets the buffer name, and the next `apply_edit` (an Edit) looks that
buffer up BY THE EXACT `:p` NAME (s:goto's by-number lookup). A wrong
name there is invisible to `show_fresh` itself and only surfaces as the
Edit missing the buffer and opening a duplicate tab.
"""

from __future__ import annotations

import io
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_goto_file_escaping import _landing
from test_integration_relock_autocmd import _keys, follower, state, vim_pane
from test_integration_tab_eviction import _start_follower

from vim_ai_follower import cli
from vim_ai_follower.backends import NavigationFailed

pytestmark = pytest.mark.integration


def _hook(monkeypatch: pytest.MonkeyPatch, phase: str, tool: str, target: Path) -> None:
    payload = json.dumps({"tool_name": tool, "tool_input": {"file_path": str(target)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert cli.main(["hook", phase]) == 0


def test_show_fresh_names_the_buffer_exactly_and_edit_reuses_it(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    follower_pane = _start_follower(tmux_session, monkeypatch, wait_until)
    dump = tmp_path / "dump.txt"
    target = tmp_path / "a b#1%.py"
    want = os.path.realpath(target)

    _hook(monkeypatch, "pre", "Write", target)
    target.write_text("one = 1\n")
    _hook(monkeypatch, "post", "Write", target)  # fresh -> show_fresh

    tabs, current, buffers = _landing(follower_pane, dump, wait_until)
    assert os.path.realpath(current) == want, f"landed on {current!r}, buffers {buffers}"
    assert [b for b in buffers if os.path.realpath(b) == want] == [current], (
        f"show_fresh's buffer name does not match the real path; buffers seen: {buffers}"
    )

    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text("one = 2\n")
    _hook(monkeypatch, "post", "Edit", target)  # apply_edit -> goto_file by-number lookup

    tabs_again, current_again, buffers_again = _landing(follower_pane, dump, wait_until)
    assert os.path.realpath(current_again) == want, f"buffers after Edit: {buffers_again}"
    assert tabs_again == tabs, (
        "the Edit could not find show_fresh's buffer by name and opened a duplicate tab "
        f"(tabs {tabs} -> {tabs_again}); buffers: {buffers_again}"
    )


def test_show_fresh_over_a_start_screen_makes_a_listed_buffer_that_survives_leaving_it(
    tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    """A dedicated follower's first file is renamed onto its current buffer,
    which can be a plugin's start screen (startify sets buftype=nofile,
    bufhidden=wipe, nobuflisted, nomodifiable). The rename kept bufhidden and
    nobuflisted: the file's buffer was missing from `:ls`, and leaving its
    window wiped it."""
    pane = vim_pane(
        tmux_session,
        tmp_path,
        wait_until,
        "setlocal buftype=nofile bufhidden=wipe nobuflisted nomodifiable",
    )
    target = tmp_path / "t.py"
    target.write_text("x = 1\n")

    assert follower(pane, adopted=False).show_fresh(str(target), "x = 1\n").outcome == "completed"

    number = state(pane, tmp_path, wait_until, "bufnr('%')")[2]
    after = state(
        pane,
        tmp_path,
        wait_until,
        f"execute('enew') . bufexists({number}) . buflisted({number})",
    )
    assert after[2] == "11", after


def test_a_rename_in_place_that_does_not_land_leaves_every_buffers_swap_alone(
    tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    """A dedicated follower's first file renames its current buffer in
    place, and swap goes off BEFORE the rename (a swap-enabled buffer renamed
    onto a swap-held name raises E325). A user autocommand that jumps on
    BufFilePre makes Vim refuse the rename and leaves another buffer
    current: nothing lands, and the buffer whose swap went off must get it
    back, as must the one the jump landed on keep its own."""
    user_file = tmp_path / "a.txt"
    user_file.write_text("user\n")
    pane = vim_pane(tmux_session, tmp_path, wait_until, f"edit {user_file}")
    _keys(pane, ":tabnew")
    _keys(pane, ":autocmd BufFilePre * tabfirst")
    target = tmp_path / "t.py"
    target.write_text("x = 1\n")

    with pytest.raises(NavigationFailed):
        follower(pane, adopted=False).show_fresh(str(target), "x = 1\n")

    swaps = state(
        pane,
        tmp_path,
        wait_until,
        "join(map(range(1, bufnr('$')), 'getbufvar(v:val, \"&swapfile\")'), '')",
    )
    assert swaps[2] == "11", swaps
