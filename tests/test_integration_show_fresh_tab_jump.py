"""Real tmux+vim proof that show_fresh renames only the buffer its own
`:tabnew` made, in an ADOPTED Vim whose user has an autocommand that jumps
tabs on a new empty buffer.

Final review of backlog-sweep-3 (I1), reproduced at acce2b2: with
`autocmd BufEnter * if bufname('%') ==# '' && tabpagenr('$') > 1 | tabfirst`
the tabnew's BufEnter took the cursor back to the user's tab, the rename
named the USER's buffer after Claude's file (so the landing check, which
compares the current buffer with the target, said "landed") and the
read-and-clear plus the retype replaced the user's unsaved text. Now the
rename runs only on a buffer proven new (a number above every earlier one,
unnamed, unmodified); otherwise nothing acts and the navigation fails safe.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_same_file import _keys, _state, _tmux, _user_vim

from vim_ai_follower.backends import NavigationFailed
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration


def _adopted_with_autocmd(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, root: Path, autocmd: str
) -> tuple[str, TmuxVimFollower]:
    (root / "user.txt").write_text("user line\n")
    pane = _user_vim(tmux_session, monkeypatch, root, "user.txt")
    _keys(pane, "-l", ":" + autocmd)
    _keys(pane, "Enter")
    _keys(pane, "-l", "GoUSER UNSAVED")
    _keys(pane, "Escape")
    window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
    FollowerState.set(
        window_id, backend="tmux", target=pane, adopted=True, speed="instant", shown_any=True
    )
    return pane, TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id=window_id)


def test_a_tabnew_that_jumps_back_never_renames_the_users_buffer(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    root = Path(os.path.realpath(tmp_path))
    target = root / "jump.py"
    target.write_text("jump = 1\n")
    pane, follower = _adopted_with_autocmd(
        tmux_session,
        monkeypatch,
        root,
        "autocmd BufEnter * if bufname('%') ==# '' && tabpagenr('$') > 1 | tabfirst | endif",
    )

    with pytest.raises(NavigationFailed):
        follower.show_fresh(str(target), "jump = 1\n", in_new_tab=True)

    state = _state(pane, tmp_path / "s.txt", wait_until)
    assert state["BUF"] == "user.txt", state
    assert state["LINES"] == ["user line", "USER UNSAVED"], state
    assert state["FILES"] == str(root / "user.txt"), state
    assert state["TABS"] == "1", state  # the tabnew's empty tab is gone too


def test_a_tab_switch_on_tabenter_still_animates_into_the_new_tab(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The reviewer's harmless variant: TabEnter fires before the new buffer
    exists, so the jump lands nowhere new and the retype goes ahead."""
    root = Path(os.path.realpath(tmp_path))
    target = root / "jump.py"
    target.write_text("jump = 1\n")
    pane, follower = _adopted_with_autocmd(
        tmux_session,
        monkeypatch,
        root,
        "autocmd TabEnter * if bufname('%') ==# '' | tabfirst | endif",
    )

    assert follower.show_fresh(str(target), "jump = 1\n", in_new_tab=True).outcome == "completed"

    state = _state(pane, tmp_path / "s.txt", wait_until)
    assert state["BUF"] == "jump.py", state
    assert state["LINES"] == ["jump = 1"], state
    files = str(state["FILES"]).split(",")
    assert str(root / "user.txt") in files, state


@pytest.mark.parametrize("in_new_tab", [True, False], ids=["new-tab", "in-place"])
def test_a_rename_that_fails_after_the_proof_leaves_no_stray_tab(
    in_new_tab: bool,
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The proof passed (the tabnew's own buffer), and then `:file` failed:
    here a user BufFilePre autocommand throws (E95 and friends fail the same
    way). The unnamed tab the tabnew made stayed open, one per Read. Now it
    goes, exactly as when the proof fails; in place, the buffer whose swap
    the rename turned off gets it back."""
    root = Path(os.path.realpath(tmp_path))
    target = root / "fails.py"
    target.write_text("fails = 1\n")
    pane, follower = _adopted_with_autocmd(
        tmux_session, monkeypatch, root, "autocmd BufFilePre * throw 'no rename'"
    )
    before = _state(pane, tmp_path / "before.txt", wait_until)

    with pytest.raises(NavigationFailed):
        follower.show_fresh(str(target), "fails = 1\n", in_new_tab=in_new_tab)

    state = _state(pane, tmp_path / "after.txt", wait_until)
    assert state["TABS"] == before["TABS"] == "1", state
    assert state["BUF"] == "user.txt", state
    assert state["LINES"] == ["user line", "USER UNSAVED"], state
    swap = tmp_path / "swap.txt"
    _keys(pane, "-l", "--", f":call writefile(['S' . &l:swapfile], '{swap}')")
    _keys(pane, "Enter")
    assert wait_until(lambda: swap.exists() and swap.read_text().startswith("S"), timeout=10.0)
    assert swap.read_text().strip() == "S1"


def test_a_throw_after_the_rename_still_animates_into_the_new_tab(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """BufFilePost throws once the buffer already has the target's name: it
    IS the target, so the caught throw lands and the retype goes ahead."""
    root = Path(os.path.realpath(tmp_path))
    target = root / "post.py"
    target.write_text("post = 1\n")
    pane, follower = _adopted_with_autocmd(
        tmux_session, monkeypatch, root, "autocmd BufFilePost * throw 'after'"
    )

    assert follower.show_fresh(str(target), "post = 1\n", in_new_tab=True).outcome == "completed"

    state = _state(pane, tmp_path / "s.txt", wait_until)
    assert state["BUF"] == "post.py", state
    assert state["LINES"] == ["post = 1"], state
    assert state["TABS"] == "2", state
