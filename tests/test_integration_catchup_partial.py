"""Real-tmux+vim twin of test_nvim_integration_catchup_partial.py: proves the
crash-fallback catch-up (hooks._consume_pending_catchup) rebuilds from the
persisted `partial` against a REAL tmux pane driving a REAL Vim, for the tmux
backend's animation drivers (src/vim_ai_follower/backends/tmux_vim.py +
animate.run_lines/run_ops).

Unlike nvim, a stop on tmux never lands mid-character: each line/op is sent as
whole KeySequence units (animate._line_sequences/_insert_sequences), and BOTH
a pause and an interrupt roll the in-flight line/op back (Escape, Escape, then
`u` undo(s)) before persisting or returning — see run_lines/run_ops. So the
live tmux buffer is always at a clean line/op boundary by the time a hook
could be killed, never the half-typed tail the nvim fix defends against. What
these tests pin down instead: the tmux drivers now DO persist a `partial`
(test_tmux_partial.py, landed in the commit right before this one), and
_consume_pending_catchup correctly rebuilds-then-resumes from it end to end
against a real editor — plus the legacy (partial=None) fallback the tmux
drivers themselves hit whenever a caller has no `before` snapshot to hand
over, which still converges as long as the live buffer sits at that clean
boundary.

The tmux drivers persist their pending file at PAUSE time, inside
_wait_while_paused's wait loop — not at the moment of a plain interrupt (see
animate._wait_while_paused). The process a real killed hook leaves behind is
one that paused, then died while blocked in that loop before its own
resume/interrupt housekeeping (the loop's `finally`, which discards the
pending file) could run. To simulate that here without an actual process
kill, each test pauses, snapshots the pending exactly as it sits on disk at
that instant (control.load_pending_animation, called from inside the
check_signal double itself, before anything decides what happens next), lets
a second signal (interrupt) unblock the call — which then runs the same
discard an ordinary interrupt would — and re-saves the snapshot by hand,
standing in for the file a real kill would have left untouched.

Measured while writing these tests, two things that are NOT the mechanism
under test but easy to mistake for it:

1. `goto_file`'s `:tab drop {file}` (used by both `rewrite_buffer` and
   `resume`) DOES hit a real `E37: No write since last change` the moment it
   targets the CURRENT tab's own dirty buffer — exactly the state an
   abandoned/killed animation leaves. It is transient, not fatal: the very
   next thing every caller sends is another `:`-prefixed ex command
   (`_UNLOCK_FOR_ANIMATION`), and Vim's hit-enter prompt treats a leading `:`
   as "dismiss and open the command line" rather than swallowing it, so the
   animation still lands on the exact intended content.
2. Every relock (`_RELOCK_SYNCED`/`_RELOCK_READONLY_SYNCED`) ends with a
   `:silent! e!` disk sync. A real crashed animation always finds the FULL
   intended content already on disk (Claude writes it before the hook even
   starts), so once that final relock fires, the buffer converges to the
   right answer via the disk sync REGARDLESS of whether the retype in
   between was correct — a broken partial would go completely undetected by
   an assertion made only after that relock. Each test below therefore also
   snapshots the pane straight after run_lines/run_ops returns and BEFORE
   the relock that follows it (patching the names `rewrite_buffer` and
   `resume` call through their own module, so the wrapper sees exactly what
   the animation itself produced) — that snapshot is the one a wrong
   partial or a stranded row would actually show up in; the final,
   post-relock content is kept as a secondary sanity check that nothing got
   left in a stuck, unrecoverable state (a hung hit-enter prompt, say).
   Confirmed by deliberately truncating a persisted partial by one line: the
   pre-relock snapshot then shows the missing line and fails, while the
   post-relock content alone would have passed regardless (see this
   session's report)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from vim_ai_follower import cli, control, hooks
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.animate import run_lines as _real_run_lines
from vim_ai_follower.animate import run_ops as _real_run_ops
from vim_ai_follower.backends import tmux_vim as tmux_vim_module
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import compute_edit_script

pytestmark = pytest.mark.integration


def _pane_ids(session_name: str) -> list[str]:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def _window_id(pane_id: str) -> str:
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{window_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _capture(pane_id: str) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", pane_id, "-p"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _rows(pane_id: str) -> list[str]:
    return [line.rstrip() for line in _capture(pane_id).splitlines()]


def _pause_then_abandon(at: int, seen: list[Any]) -> Callable[..., str | None]:
    """check_signal double: pause at the `at`-th call — landing inside the
    line/op the driver is currently sending — snapshot whatever the wait
    loop persisted on the very next call (its first poll), then interrupt to
    unblock the run. The interrupt's own housekeeping
    (animate._wait_while_paused's `finally`) discards that pending file same
    as a real interrupt would; the snapshot captured just before it is what a
    real kill would have left on disk instead, and the caller re-saves it by
    hand to stand in for that."""
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == at:
            return "pause"
        if calls["n"] == at + 1:
            seen.append(control.load_pending_animation(window_id, base_dir))
            return "interrupt"
        return None

    return _check


def _patch_pre_relock_snapshots(
    monkeypatch: pytest.MonkeyPatch, pane_id: str, snapshots: list[list[str]]
) -> None:
    """Record the pane's rows immediately after each run_lines/run_ops call
    made through the tmux backend — i.e. exactly what the animation itself
    produced, one entry per call, in order, captured BEFORE the relock that
    _with_unlocked sends right after (see the module docstring for why the
    relock's own `:silent! e!` would otherwise mask a wrong retype)."""

    def _wrapped_run_lines(*args: Any, **kwargs: Any) -> AnimationResult:
        result = _real_run_lines(*args, **kwargs)
        snapshots.append(_rows(pane_id))
        return result

    def _wrapped_run_ops(*args: Any, **kwargs: Any) -> AnimationResult:
        result = _real_run_ops(*args, **kwargs)
        snapshots.append(_rows(pane_id))
        return result

    monkeypatch.setattr(tmux_vim_module, "run_lines", _wrapped_run_lines)
    monkeypatch.setattr(tmux_vim_module, "run_ops", _wrapped_run_ops)


def test_show_fresh_catchup_after_a_paused_and_abandoned_run_reaches_full_content(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)
    window_id = _window_id(origin_pane)

    file_a_path = tmp_path / "a.py"
    content = "alpha\nbeta\ngamma\ndelta\n"
    # A real crashed animation always finds this already on disk — Claude
    # writes the file in full before the hook's animation even starts (see
    # test_integration.py's own comment to this effect). Skipping this write
    # would make the eventual relock's `:silent! e!` reload a NONEXISTENT
    # file, which empties the buffer instead of syncing it — a test-fidelity
    # bug, not the thing under test.
    file_a_path.write_text(content)
    file_a = str(file_a_path)

    follower = TmuxVimFollower(pane_id=follower_pane_id, window_id=window_id)
    seen: list[Any] = []
    # 4 checks per line (opener, text, Escape, Escape): pausing at the 9th
    # call lands right at line index 2's opener, before any of its keystrokes
    # are sent — lines 0 and 1 ("alpha", "beta") are already fully typed.
    monkeypatch.setattr(control, "check_signal", _pause_then_abandon(9, seen))
    result = follower.show_fresh(file_a, content)
    assert result == AnimationResult("interrupted", 2)
    # The interrupt-unblock's own housekeeping already discarded it — proof
    # this abandoned-hook simulation genuinely needs the by-hand re-save
    # below, exactly like the nvim twin's explicit control.save_pending_*.
    assert not control.has_pending_animation(window_id)

    pending = seen[0]
    assert isinstance(pending, control.PendingShowFresh)
    assert pending.lines == ("gamma", "delta")
    assert pending.continuation is True
    assert pending.file_path == file_a
    assert pending.partial == "alpha\nbeta\n"

    control.save_pending_show_fresh(
        window_id,
        pending.lines,
        0.0,
        continuation=pending.continuation,
        file_path=pending.file_path,
        partial=pending.partial,
    )

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    reloaded = control.load_pending_animation(window_id)
    assert reloaded is not None
    snapshots: list[list[str]] = []
    _patch_pre_relock_snapshots(monkeypatch, follower_pane_id, snapshots)
    catchup_follower = TmuxVimFollower(pane_id=follower_pane_id, window_id=window_id)
    hooks._consume_pending_catchup(catchup_follower, file_a, reloaded)

    # snapshots[0]: rewrite_buffer's retype of the partial alone, pre-relock
    # (rewrite_buffer never relocks itself). snapshots[1]: resume's
    # continuation typing "gamma"/"delta", pre-relock — the disk-sync-immune
    # proof the full content was actually typed, not merely re-synced.
    assert len(snapshots) == 2
    partial_start = snapshots[0].index("alpha")
    assert snapshots[0][partial_start : partial_start + 2] == ["alpha", "beta"]
    final_start = snapshots[1].index("alpha")
    assert snapshots[1][final_start : final_start + 4] == ["alpha", "beta", "gamma", "delta"]

    # Secondary sanity check: the relock's own disk sync leaves the pane
    # settled on the same content, not stuck on an unresolved prompt.
    assert wait_until(lambda: "delta" in _capture(follower_pane_id), timeout=10.0)
    rows = _rows(follower_pane_id)
    start = rows.index("alpha")
    assert rows[start : start + 4] == ["alpha", "beta", "gamma", "delta"]


def test_apply_edit_catchup_after_a_paused_and_abandoned_mid_op_run_reaches_full_content(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    # Same class, PendingApplyEdit side. The op replaces one line with THREE
    # ("b" -> "WW","XX","YY"), and the pause lands after "WW" is typed but
    # before "XX" — the multi-line shape the nvim twin also exercises,
    # confirmed by measurement below that the tmux rollback still collapses
    # this back to a clean pre-op boundary (a single-line leftover would have
    # converged by coincidence even with a broken rollback).
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)
    window_id = _window_id(origin_pane)

    file_a_path = tmp_path / "a.py"
    before = "a\nb\nc\n"
    after = "a\nWW\nXX\nYY\nc\n"
    # Same fidelity requirement as the show_fresh twin above: the file is
    # already on disk with its FINAL content by the time an edit's animation
    # starts (Claude writes it first), so it must hold `after`, not `before`,
    # once the apply_edit under test begins.
    file_a_path.write_text(before)
    file_a = str(file_a_path)
    TmuxVimFollower(pane_id=follower_pane_id, window_id=window_id).show_fresh(file_a, before)
    file_a_path.write_text(after)

    ops = compute_edit_script(before, after)
    assert len(ops) == 1  # a single multi-line replace, like the nvim twin

    follower = TmuxVimFollower(pane_id=follower_pane_id, window_id=window_id)
    seen: list[Any] = []
    # delete half: ":2d", Enter -> 2 checks. insert half: ":1", Enter, "o",
    # "WW", Enter, "XX", Enter, "YY", Escape, Escape -> 10 checks. Pausing at
    # the 8th overall call lands right before "XX" is sent, i.e. after "WW"
    # (and the Enter that opened its line) already landed.
    monkeypatch.setattr(control, "check_signal", _pause_then_abandon(8, seen))
    result = follower.apply_edit(file_a, ops, before=before)
    assert result == AnimationResult("interrupted", 0)
    assert not control.has_pending_animation(window_id)

    pending = seen[0]
    assert isinstance(pending, control.PendingApplyEdit)
    assert pending.ops == ops
    assert pending.file_path == file_a
    # apply_ops(before, []) == before with no ops applied, in the canonical
    # terminated form every persisted partial uses — the whole op was still
    # in flight, so nothing of it is in the persisted base yet.
    assert pending.partial == "a\nb\nc\n"

    control.save_pending_apply_edit(
        window_id, pending.ops, 0.0, file_path=pending.file_path, partial=pending.partial
    )

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    reloaded = control.load_pending_animation(window_id)
    assert reloaded is not None
    snapshots: list[list[str]] = []
    _patch_pre_relock_snapshots(monkeypatch, follower_pane_id, snapshots)
    catchup_follower = TmuxVimFollower(pane_id=follower_pane_id, window_id=window_id)
    hooks._consume_pending_catchup(catchup_follower, file_a, reloaded)

    # snapshots[0]: rewrite_buffer's retype of "a\nb\nc" (the persisted
    # partial), pre-relock. snapshots[1]: resume's re-run of the whole op,
    # pre-relock — disk-sync-immune proof it actually reached "WW"/"XX"/"YY".
    assert len(snapshots) == 2
    partial_start = snapshots[0].index("a")
    assert snapshots[0][partial_start : partial_start + 3] == ["a", "b", "c"]
    final_start = snapshots[1].index("a")
    assert snapshots[1][final_start : final_start + 5] == ["a", "WW", "XX", "YY", "c"]

    assert wait_until(lambda: "YY" in _capture(follower_pane_id), timeout=10.0)
    rows = _rows(follower_pane_id)
    start = rows.index("a")
    assert rows[start : start + 5] == ["a", "WW", "XX", "YY", "c"]


def test_catchup_from_a_legacy_pending_without_a_partial_converges_at_a_clean_boundary(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    # Backward compatibility: a pending file written before `partial` existed
    # loads with partial=None, which means "not recorded" — the catch-up then
    # keeps trusting the live buffer, exactly as it always did. On tmux the
    # live buffer is always at a clean boundary after any stop, so that trust
    # is warranted; this pins that the fallback path itself still converges.
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)
    window_id = _window_id(origin_pane)

    file_c_path = tmp_path / "c.py"
    content = "one\ntwo\nthree\n"
    file_c_path.write_text(content)  # see the show_fresh twin above for why
    file_c = str(file_c_path)

    follower = TmuxVimFollower(pane_id=follower_pane_id, window_id=window_id)
    seen: list[Any] = []
    # Pausing at the 5th call lands exactly on line index 1's opener, before
    # any of its keystrokes are sent: line 0 ("one") is already a clean,
    # fully-typed boundary with nothing left for the rollback to undo.
    monkeypatch.setattr(control, "check_signal", _pause_then_abandon(5, seen))
    result = follower.show_fresh(file_c, content)
    assert result == AnimationResult("interrupted", 1)
    assert not control.has_pending_animation(window_id)

    # Simulate a pending file written before `partial` existed: the same
    # remainder, with no partial recorded at all.
    control.save_pending_show_fresh(
        window_id, ("two", "three"), 0.0, continuation=True, file_path=file_c
    )
    reloaded = control.load_pending_animation(window_id)
    assert reloaded is not None
    assert reloaded.partial is None

    monkeypatch.setattr(control, "check_signal", lambda *a, **k: None)
    snapshots: list[list[str]] = []
    _patch_pre_relock_snapshots(monkeypatch, follower_pane_id, snapshots)
    catchup_follower = TmuxVimFollower(pane_id=follower_pane_id, window_id=window_id)
    hooks._consume_pending_catchup(catchup_follower, file_c, reloaded)

    # partial=None skips rewrite_buffer entirely (see _consume_pending_catchup),
    # so only resume's own run_lines call fires — one snapshot, pre-relock.
    assert len(snapshots) == 1
    final_start = snapshots[0].index("one")
    assert snapshots[0][final_start : final_start + 3] == ["one", "two", "three"]

    assert wait_until(lambda: "three" in _capture(follower_pane_id), timeout=10.0)
    rows = _rows(follower_pane_id)
    start = rows.index("one")
    assert rows[start : start + 3] == ["one", "two", "three"]
