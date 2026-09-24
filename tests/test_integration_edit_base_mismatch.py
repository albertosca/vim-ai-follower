"""Real tmux+vim proof of the base-mismatch guard: an Edit's diff is typed
only onto a buffer that holds the diff's base (the pre-edit snapshot);
anything else is retyped in full, and the finished file never flashes first.

Same instrument as tests/test_integration_edit_no_reload.py (whose helpers
this reuses): a 15 ms Vim timer loaded through VIMINIT logs every distinct
state of the REAL buffer mid-animation without sending a key. A missed sample
can hide a violation but never invent one.

The two measured ways the buffer is not the base:

1. Listed but UNLOADED — the user closed its tab under 'nohidden'. The file
   is still in open_files, so the edit was not "fresh", and goto_file's
   `:tab sbuffer N` loaded the finished file from disk before the diff was
   typed on top of it: the same flash-then-duplicate the reload bug had.
2. Open, CLEAN, and changed on disk outside Claude (a formatter). The buffer
   still holds the old text while the ops were computed against the new
   one, so they landed on the wrong base mid-animation; only the relock's
   `:e!` repaired the end state.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_edit_no_reload import (
    _MARK,
    ONE_DEF,
    OTHER,
    THREE_DEFS,
    _defs,
    _edit_and_assert_typed_from_the_pre_edit_buffer,
    _hook,
    _last_state,
    _observed_follower,
    _states,
    _write,
)

from vim_ai_follower.backends.tmux_vim import TmuxVimFollower

pytestmark = pytest.mark.integration

# ONE_DEF after a formatter: same structure, different quotes. The stale
# line is the single-quoted one; it can only coexist with a `def bravo` if
# the diff was typed onto the stale buffer.
STALE_LINE = "    return os.path.join(root, 'a')"
FORMATTED = ONE_DEF.replace("'a'", '"a"')
FORMATTED_THREE = THREE_DEFS.replace("'a'", '"a"')


def _keys(pane: str, *commands: str) -> None:
    for command in commands:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", command], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)


def _unload_a_behind_b(
    follower_pane: str, target: Path, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    """a.py is tab 1, b.py tab 2 (current): close a.py's tab under
    'nohidden', which unloads its buffer but keeps it listed."""
    _keys(follower_pane, ":set nohidden", ":1tabclose")
    probe = tmp_path / "setup-probe.txt"
    _keys(
        follower_pane,
        f":call writefile([tabpagenr('$'), bufloaded('{target}'), buflisted('{target}')],"
        f" '{probe}')",
    )
    assert wait_until(lambda: probe.exists() and probe.read_text() == "1\n0\n1\n", timeout=5.0), (
        f"setup failed: a.py should be listed but unloaded: {probe.read_text()!r}"
        if probe.exists()
        else "setup failed: probe never written"
    )


def test_edit_of_a_listed_but_unloaded_file_is_retyped_not_flashed(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    log, follower_pane, _ = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    _write(monkeypatch, log, wait_until, target, ONE_DEF)
    _write(monkeypatch, log, wait_until, tmp_path / "b.py", OTHER)
    _unload_a_behind_b(follower_pane, target, tmp_path, wait_until)
    _edit_and_assert_typed_from_the_pre_edit_buffer(monkeypatch, log, wait_until, target)


def test_edit_after_an_outside_rewrite_never_types_the_diff_onto_the_stale_buffer(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    log, _, _ = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    _write(monkeypatch, log, wait_until, target, ONE_DEF)
    target.write_text(FORMATTED)  # a formatter, not Claude: the buffer is now stale

    after = FORMATTED_THREE.splitlines()
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text(FORMATTED_THREE)
    _hook(monkeypatch, "post", "Edit", target)
    assert wait_until(lambda: _last_state(log, target.name) == after, timeout=15.0)

    states = _states(log, target.name)
    trace = "\n".join(f"defs={_defs(s)} lines={len(s)} stale={STALE_LINE in s}" for s in states)
    assert len(states) >= 2, f"observer caught no intermediate state:\n{trace}"
    # Wrong base, concretely: the stale buffer's single-quoted line together
    # with a def the diff added. The retype wipes the stale text first, so
    # the two can never be on screen together.
    mixed = [s for s in states if STALE_LINE in s and any("def bravo" in line for line in s)]
    assert not mixed, f"the diff was typed onto the stale buffer:\n{trace}"
    assert after not in states[:-1], f"finished file shown before the animation ended:\n{trace}"
    assert max(_defs(s) for s in states) <= _defs(after), f"duplicated defs:\n{trace}"


def test_buffer_holds_answers_from_the_real_vim_buffer(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The Vim half of the probe, end to end: loaded and matching, loaded
    and stale, listed but unloaded, and never opened."""
    log, follower_pane, _ = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    _write(monkeypatch, log, wait_until, target, ONE_DEF)
    _write(monkeypatch, log, wait_until, tmp_path / "b.py", OTHER)
    follower = TmuxVimFollower(pane_id=follower_pane)

    assert follower.buffer_holds(str(target), ONE_DEF) is True
    assert follower.buffer_holds(str(target), FORMATTED) is False
    assert follower.buffer_holds(str(tmp_path / "never.py"), "") is False
    _unload_a_behind_b(follower_pane, target, tmp_path, wait_until)
    assert follower.buffer_holds(str(target), ONE_DEF) is False
