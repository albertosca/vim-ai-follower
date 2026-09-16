"""The tmux twin of test_nvim_integration_partial_form.py: a real Vim, driven
through tmux send-keys, must get back every line of a persisted `partial` —
including a trailing blank one.

Two things shape this file.

**The instrument.** `tmux capture-pane` returns a rendered SCREEN, which cannot
be trusted to tell a genuine trailing blank line apart from the empty rows
below the buffer. These tests dump the real buffer with `:w! <path>` and read
that file, and each test dumps TWICE with different expected contents, so the
instrument is shown to distinguish rather than assumed to.

**Where the measurement can land.** The tmux backend relocks with
`:silent! e! | setlocal ...` — a reload from disk. Asserting the buffer after a
COMPLETED apply_edit/resume would therefore measure the file Claude already
wrote, and would pass just as happily with the partial one line short: a
criterion that cannot fail is not a check. So both measurements land where no
relock has run — right after `rewrite_buffer` (which documents that it leaves
the buffer unlocked for the resume that follows) and right after an INTERRUPTED
resume (`_with_unlocked` only relocks on a non-interrupted outcome).

Reuses tests/test_integration.py's pane helpers by import, and conftest.py's
`tmux_session` / `wait_until` fixtures.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration import _pane_ids, _window_id

from vim_ai_follower import cli, control
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.control import PendingApplyEdit
from vim_ai_follower.diff import apply_ops, compute_edit_script
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.integration

# Three separate hunks, so the remainder still holds two ops after the first
# one lands — that is what makes an interrupt-after-one-op possible below.
# The BOTTOM hunk (applied first) turns the last line into a blank one.
_BEFORE = "one\ntwo\nthree\nfour\nfive\n"
_AFTER = "ONE\ntwo\nTHREE\nfour\n\n"
_OPS = compute_edit_script(_BEFORE, _AFTER)

# The two expected buffer states are written out as LITERALS on purpose: they
# are the ground truth about what Vim must be holding, independent of the
# function under test. The tests feed Vim `apply_ops(...)` and compare against
# these — asserting the buffer against another apply_ops call would be circular
# and would stay green with the round-trip loss back in place.
_PREFIX = "one\ntwo\nthree\nfour\n\n"  # after the bottom hunk: blank last line
_REPLAYED_ONE = "one\ntwo\nTHREE\nfour\n\n"  # ...plus the remainder's first op


def test_the_scenario_is_not_vacuous() -> None:
    assert len(_OPS) == 3  # a shorter script could not leave a two-op remainder
    assert _OPS[0].new_lines == ("",)
    assert _PREFIX.splitlines() == ["one", "two", "three", "four", ""]
    assert _REPLAYED_ONE.splitlines() == ["one", "two", "THREE", "four", ""]


def _follower_pane(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, wait_until: Callable[..., bool]
) -> tuple[TmuxVimFollower, str]:
    origin = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    pane_id = next(p for p in _pane_ids(tmux_session) if p != origin)
    follower = TmuxVimFollower(pane_id=pane_id, window_id=_window_id(origin), pace_seconds=0.0)
    return follower, pane_id


def _assert_buffer(
    pane_id: str, dest: Path, expected: str, wait_until: Callable[..., bool]
) -> None:
    """Dump the live Vim buffer to `dest` with `:w!` and wait for it to hold
    exactly `expected`. `:w!` writes the buffer verbatim — one "\\n" per line,
    so a blank last line shows up as a real trailing newline."""
    pane = TmuxPane(pane_id=pane_id)
    pane.send_key("Escape")
    pane.send_key("Escape")
    pane.send_text(f":w! {dest}")
    pane.send_key("Enter")
    matched = wait_until(lambda: dest.exists() and dest.read_text() == expected, timeout=10.0)
    actual = dest.read_text() if dest.exists() else "<no dump written>"
    assert matched, f"buffer dump was {actual!r}, expected {expected!r}"


def test_rewrite_buffer_rebuilds_a_blank_ending_prefix_line_for_line(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    follower, pane_id = _follower_pane(tmux_session, monkeypatch, wait_until)
    target = tmp_path / "a.txt"
    target.write_text(_BEFORE)

    follower.show_fresh(str(target), _BEFORE)
    # First reading: proves the dump reflects the real buffer, and gives the
    # instrument a second, different value to be judged against.
    _assert_buffer(pane_id, tmp_path / "dump-before", _BEFORE, wait_until)

    # Exactly what hooks._replay_remainder / _consume_pending_catchup do with a
    # persisted partial: the string a saver would have recorded, handed to
    # rewrite_buffer. No relock runs after this, so what is dumped is what the
    # rebuild really produced.
    partial = apply_ops(_BEFORE, _OPS[:1])
    assert follower.rewrite_buffer(str(target), partial).outcome == "completed"
    _assert_buffer(pane_id, tmp_path / "dump-prefix", _PREFIX, wait_until)


def test_the_des_interrupt_replay_edits_the_rebuilt_prefix_at_the_right_lines(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    # The rebuild is only half the story: the remainder's line numbers are
    # relative to the prefix, so a prefix one line short would also mis-place
    # every op replayed onto it.
    follower, pane_id = _follower_pane(tmux_session, monkeypatch, wait_until)
    target = tmp_path / "a.txt"
    target.write_text(_BEFORE)

    follower.show_fresh(str(target), _BEFORE)
    partial = apply_ops(_BEFORE, _OPS[:1])
    assert follower.rewrite_buffer(str(target), partial).outcome == "completed"
    _assert_buffer(pane_id, tmp_path / "dump-prefix", _PREFIX, wait_until)

    # Signal check 9 interrupts right after the remainder's FIRST op with
    # nothing rolled back (measured with a throwaway probe over 1..13; 1-8 stop
    # inside that op and undo it). An interrupted outcome skips the relock, so
    # the dump below is the animation's own result, not a reload from disk.
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "interrupt" if calls["n"] == 9 else None

    monkeypatch.setattr(control, "check_signal", _check)
    pending = PendingApplyEdit(_OPS[1:], 0.0, file_path=str(target), partial=partial)
    result = follower.resume(pending)
    assert result.outcome == "interrupted"
    assert result.completed_count == 1

    _assert_buffer(pane_id, tmp_path / "dump-replayed", _REPLAYED_ONE, wait_until)
