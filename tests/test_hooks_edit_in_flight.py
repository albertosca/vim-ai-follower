"""A Read that lands between an Edit's pre and post hooks (BACKLOG D2).

Two agents on one window: agent A's Edit ran its pre hook and wrote the file;
agent B's Read of the same file posts before A's post hook. No animation slot
is held from pre to post, so the Read re-read the clean buffer — the finished
file on screen — and A's post found a buffer that was no longer its base
(retyped on a dedicated follower, left alone with a cue on an adopted one).

The pre hook now marks the edit in flight next to its snapshot; a Read of that
file skips while the mark is live AND the file on disk is no longer the
snapshot. The mark is cleared by the post hook, and it expires, because an
Edit that is denied or fails never runs a post hook.

The real-editor proofs (a mid-animation observer on tmux+Vim and on nvim) are
in tests/test_integration_edit_no_reload.py and
tests/test_nvim_integration_edit_base_mismatch.py.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from helpers import make_mock_tmux_run as _mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import control, hooks, snapshot, state

_ENV = {"TMUX_PANE": "%1"}


def _payload(tool: str, target: Path) -> dict[str, Any]:
    return {"tool_name": tool, "tool_input": {"file_path": str(target)}}


def _pre(target: Path) -> None:
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        assert hooks.cmd_hook_pre(_ENV, _payload("Edit", target)) == 0


def _read(target: Path) -> list[list[str]]:
    """Run a Read post hook against a fake tmux; the send-keys it issued."""
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post(_ENV, _payload("Read", target)) == 0
    return [
        c for c in (call.args[0] for call in run.call_args_list) if c[:2] == ["tmux", "send-keys"]
    ]


def _open_files() -> tuple[str, ...]:
    current = state.FollowerState.read("@1")
    assert current is not None
    return current.open_files


def _real(target: Path) -> str:
    return os.path.realpath(str(target))


def test_the_pre_hook_marks_the_edit_in_flight(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _pre(target)
    assert snapshot.in_flight("@1", _real(target))
    assert not snapshot.in_flight("@1", _real(tmp_path / "b.py"))


def test_a_read_of_a_file_whose_edit_is_written_but_not_posted_leaves_the_follower_alone(
    tmp_path: Path,
) -> None:
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    target.write_text("one\ntwo\n")  # the Edit tool wrote; its post has not run

    assert _read(target) == []
    assert _open_files() == ()  # untouched: the Edit's post brings the file on screen
    assert snapshot.in_flight("@1", _real(target))  # a Read never consumes the mark


def test_a_read_before_the_edit_writes_navigates_as_usual(tmp_path: Path) -> None:
    """Disk still holds the snapshot — the edit is waiting on a permission
    prompt, or was denied or failed and will never post. A re-read then shows
    exactly the edit's base, so there is nothing to protect."""
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)

    assert _read(target) != []
    assert _open_files() == (_real(target),)


def test_an_expired_mark_no_longer_holds_a_read_back(tmp_path: Path) -> None:
    """An Edit that fails after writing, or whose post hook is killed, leaves
    the mark behind; it must not silence Reads of that file forever."""
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    target.write_text("one\ntwo\n")
    mark = snapshot.in_flight_path("@1", _real(target))
    old = time.time() - snapshot.IN_FLIGHT_TTL_SECONDS - 1
    os.utime(mark, (old, old))

    assert not snapshot.in_flight("@1", _real(target))
    assert _read(target) != []
    assert _open_files() == (_real(target),)


def test_a_mark_just_inside_its_ttl_still_holds(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _pre(target)
    mark = snapshot.in_flight_path("@1", _real(target))
    recent = time.time() - snapshot.IN_FLIGHT_TTL_SECONDS + 5
    os.utime(mark, (recent, recent))
    assert snapshot.in_flight("@1", _real(target))


def test_a_read_whose_file_cannot_be_decoded_is_held_back_while_in_flight(
    tmp_path: Path,
) -> None:
    """The pre hook snapshots an undecodable file as "" — so undecodable disk
    content is never that snapshot: the edit wrote, and the Read waits."""
    target = tmp_path / "a.bin"
    target.write_bytes(b"\xff\xfe\x00")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    target.write_bytes(b"\xff\xfe\x01")

    assert _read(target) == []


def test_a_read_of_another_file_is_not_held_back(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    other = tmp_path / "b.py"
    target.write_text("one\n")
    other.write_text("other\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    target.write_text("one\ntwo\n")

    assert _read(other) != []
    assert _open_files() == (_real(other),)


def test_the_edit_post_hook_clears_the_mark_once_it_holds_the_slot(tmp_path: Path) -> None:
    """Handed over, not dropped: at the clear the slot is already held, so a
    Read is kept off the pane at every instant (cleared first, a Read landing
    between the two steps would re-read the finished file). And the mark is
    gone before the animation starts: a hook killed later — a hook timeout in
    the hand-off wait never reaches a `finally` — cannot leave it behind to
    silence Reads of the file until it expires."""
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    target.write_text("one\ntwo\n")

    slot_held_at_clear: list[bool] = []
    real_clear = snapshot.clear_in_flight

    def _clear(window_id: str, file_path: str, *args: Any, **kwargs: Any) -> None:
        slot_held_at_clear.append(control.is_animating(window_id))
        real_clear(window_id, file_path, *args, **kwargs)

    at_animation: list[tuple[bool, bool]] = []

    def _animate(*args: Any, **kwargs: Any) -> int:
        at_animation.append((snapshot.in_flight("@1", _real(target)), control.is_animating("@1")))
        return 0

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch.object(snapshot, "clear_in_flight", _clear),
        patch.object(hooks, "_animate_edit", _animate),
    ):
        assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0

    assert slot_held_at_clear == [True]
    assert at_animation == [(False, True)]
    assert _read(target) != []  # the next Read is an ordinary one again


def test_an_edit_post_that_finds_the_slot_busy_clears_the_mark_too(tmp_path: Path) -> None:
    """That edit is never animated (its file is marked stale instead), so a
    later Read re-reading the finished file is right, not a flash."""
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    target.write_text("one\ntwo\n")
    control.mark_animating("@1")  # another hook of this process's window owns the pane
    try:
        with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
            assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0
        assert control.is_animating("@1")  # the other hook's slot is not ours to release
    finally:
        control.clear_animating("@1")
    assert not snapshot.in_flight("@1", _real(target))


def test_clearing_a_mark_that_was_never_set_is_harmless(tmp_path: Path) -> None:
    snapshot.clear_in_flight("@1", str(tmp_path / "never.py"))
    assert not snapshot.in_flight("@1", str(tmp_path / "never.py"))
