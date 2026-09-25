"""Real tmux+vim proof that a pace-0 catch-up leaves the buffer at the NEW
edit's base, not at the finished file on disk.

The tmux twin of the nvim `test_a_killed_hand_off_catch_up_still_replays_onto_
an_untouched_buffer`, for an adopted and a dedicated follower alike.

A remainder outlives its hook (a killed hand-off, or a pause that outlived
it) and the next Edit of the same file replays it first, so the buffer holds
what the new edit's diff was computed against. Every completed tmux
animation used to end with the relock's `:silent! e!`, the catch-up
included. By `hook post` Claude has already written the NEW edit, so that
`:e!` loaded the finished file (reproduced 2026-09-25 by the B2 re-reviewer):

  - adopted: the base probe then saw "differs", showed the false cue, marked
    the file stale and animated nothing — the screen jumped straight to the
    finished file;
  - dedicated: the finished file flashed, then was wiped and retyped instead
    of the diff being typed.

The observer (see test_integration_edit_no_reload.py) samples the real buffer
mid-animation, because the end state is correct in both the bug and the fix.
"""

from __future__ import annotations

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

from vim_ai_follower import control
from vim_ai_follower.diff import compute_edit_script
from vim_ai_follower.hooks import BASE_DIFFERS_CUE, PROBE_UNKNOWN_CUE
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.integration

WRITTEN = "x = 1\ny = 2\n"  # what the follower animated
KILLED = "x = 1\ny = 3\n"  # the killed Edit's write: the new edit's base
FINISHED = "x = 1\ny = 3\nz = 4\n"  # the new Edit


@pytest.mark.parametrize("adopted", [True, False], ids=["adopted", "dedicated"])
def test_a_catch_up_leaves_the_new_edits_base_and_the_edit_is_typed_as_a_diff(
    adopted: bool,
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    log, pane, window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    _write(monkeypatch, log, wait_until, target, WRITTEN)
    if adopted:
        FollowerState.update(window_id, adopted=True)
    # An Edit (y = 2 -> y = 3) whose hook died holding the hand-off: disk has
    # its content, the follower's buffer still the pre-edit one, and its
    # remainder is pending with partial = what the interrupt left on screen.
    target.write_text(KILLED)
    control.save_pending_apply_edit(
        window_id,
        compute_edit_script(WRITTEN, KILLED),
        0.0,
        file_path=str(target),
        partial=WRITTEN,
    )

    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text(FINISHED)
    _hook(monkeypatch, "post", "Edit", target)

    after = FINISHED.splitlines()
    base = KILLED.splitlines()
    assert wait_until(lambda: _last_state(log, target.name) == after, timeout=15.0)
    states = _states(log, target.name)
    trace = "\n".join(repr(s) for s in states)
    # The finished file is only ever the LAST state: never a flash the
    # catch-up's relock loaded and the new edit then typed over (or wiped).
    assert after not in states[:-1], f"finished file shown before it was typed:\n{trace}"
    # The catch-up itself starts with rewrite_buffer's `:%d` (by design, the
    # buffer is rebuilt to the partial), so the no-wipe rule starts where the
    # catch-up ends: at the new edit's base.
    assert base in states, f"the catch-up never reached the new edit's base:\n{trace}"
    new_edit = states[states.index(base) :]
    # A retype wipes the buffer first; a diff keeps the base's lines throughout.
    assert all(s[:2] == base for s in new_edit), f"the buffer was wiped:\n{trace}"
    # The new edit was really typed on top of its base, not jumped to: some
    # sample shows the base with the new line partly (or not yet) typed.
    assert any(len(s) == 3 and "z = 4".startswith(s[2]) and s[2] != "z = 4" for s in new_edit), (
        f"the diff was never seen typing onto the base:\n{trace}"
    )

    title = TmuxPane(pane_id=pane).title()
    assert title not in (BASE_DIFFERS_CUE, PROBE_UNKNOWN_CUE), title
    state = FollowerState.read(window_id)
    assert state is not None
    assert str(target) not in state.stale_files
    hook_log = tmp_path / "hook.log"
    assert "left the buffer" not in (hook_log.read_text() if hook_log.exists() else "")
    assert control.load_pending_animation(window_id) is None
