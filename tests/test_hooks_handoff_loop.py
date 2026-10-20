"""More than ONE interrupt/des-interrupt cycle per animation.

Live finding (2026-09-16): a third S — landing mid-replay — used to hand the
buffer over and RETURN, so nobody listened for S any more. A fourth press did
nothing and the buffer stayed partial and modifiable until the next completed
animation's relock healed it. `_await_user_handoff` now loops: every
interrupted replay re-arms the wait with a shortened remainder and a grown
partial.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from helpers import make_mock_tmux_run as _mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import commands, control, hooks, state
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.diff import EditOp
from vim_ai_follower.session import Session

_OPS = [
    EditOp(kind="replace", start_line=2, end_line=2, new_lines=("B",)),
    EditOp(kind="replace", start_line=3, end_line=3, new_lines=("C",)),
    EditOp(kind="replace", start_line=4, end_line=4, new_lines=("D",)),
]


def _scripted(signals: list[str | None]) -> Callable[..., str | None]:
    """check_signal double: walks `signals`, then None forever — with a hard
    ceiling so a hand-off that never releases fails loudly instead of hanging."""
    calls = {"n": 0}

    def _check(window_id: str, base_directory: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] > len(signals) + 200:
            raise AssertionError("hand-off wait never released within the poll budget")
        return signals[calls["n"] - 1] if calls["n"] <= len(signals) else None

    return _check


def _spy_saves(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any, float, Any]]:
    """Record every pending remainder the hand-off persists, while still
    writing it to disk — the next cycle loads it back."""
    saved: list[tuple[str, Any, float, Any]] = []
    real_fresh = control.save_pending_show_fresh
    real_ops = control.save_pending_apply_edit

    def _fresh(
        window_id: str,
        lines: tuple[str, ...],
        pace_seconds: float,
        continuation: bool = False,
        base_directory: Path | None = None,
        file_path: str = "",
        partial: str | None = None,
    ) -> None:
        saved.append(("show_fresh", lines, pace_seconds, continuation))
        real_fresh(window_id, lines, pace_seconds, continuation, base_directory, file_path, partial)

    def _ops(
        window_id: str,
        ops: list[EditOp],
        pace_seconds: float,
        base_directory: Path | None = None,
        file_path: str = "",
        partial: str | None = None,
    ) -> None:
        saved.append(("apply_edit", list(ops), pace_seconds, file_path))
        real_ops(window_id, ops, pace_seconds, base_directory, file_path, partial)

    monkeypatch.setattr(control, "save_pending_show_fresh", _fresh)
    monkeypatch.setattr(control, "save_pending_apply_edit", _ops)
    return saved


def _install_fake_follower(monkeypatch: pytest.MonkeyPatch, fake: MagicMock) -> None:
    monkeypatch.setattr(hooks, "get_follower", lambda *arguments, **keyword_arguments: fake)
    monkeypatch.setattr(
        hooks, "status_surface_for", lambda *arguments, **keyword_arguments: MagicMock()
    )
    monkeypatch.setattr(hooks, "show_popup", lambda *arguments, **keyword_arguments: None)


def _handoff_state(tmp_path: Path, content: str) -> tuple[state.FollowerState, str]:
    target = tmp_path / "f.txt"
    target.write_text(content)
    _register_fake_follower("@1", "%2", open_files=(str(target),), shown_any=True)
    current = state.FollowerState.read("@1")
    assert current is not None
    return current, str(target)


_SESSION = Session(window_id="@1", origin="%1", in_tmux=True)


def test_show_fresh_replay_interrupted_midway_re_enters_the_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    after = "l0\nl1\nl2\nl3\n"
    current, target = _handoff_state(tmp_path, after)
    # The live interrupt already happened: l0 is on screen, l1..l3 pending.
    control.save_pending_show_fresh(
        "@1", ("l1", "l2", "l3"), 0.03, continuation=True, file_path=target
    )
    saved = _spy_saves(monkeypatch)

    fake = MagicMock()
    fake.rewrite_buffer.return_value = AnimationResult("completed", 1)
    # first replay types l1 and l2 then the user hits S again; the second one
    # finishes the single remaining line.
    fake.resume.side_effect = [
        AnimationResult("interrupted", 2),
        AnimationResult("completed", 1),
    ]
    _install_fake_follower(monkeypatch, fake)

    with (
        # FollowerState.get gates update_current_file on the pane being alive
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", _scripted(["interrupt", "interrupt"])),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        hooks._await_user_handoff(current, _SESSION, target, after, "l0\n")

    first, second = fake.resume.call_args_list
    assert first.args[0].lines == ("l1", "l2", "l3")
    assert second.args[0].lines == ("l3",)  # shortened by the two lines replayed
    assert second.kwargs["seeded"] is False
    # the buffer is rebuilt to the ACCUMULATED partial before the second replay
    assert fake.rewrite_buffer.call_args_list == [
        call(target, "l0\n"),
        call(target, "l0\nl1\nl2\n"),
    ]
    assert saved == [("show_fresh", ("l3",), 0.03, True)]
    assert fake.hand_over.call_count == 1
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file == target  # following resumed once it finished
    assert capsys.readouterr().out == ""


def test_apply_edit_replay_interrupted_midway_re_enters_the_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    after = "a\nB\nC\nD\n"
    current, target = _handoff_state(tmp_path, after)
    control.save_pending_apply_edit("@1", _OPS, 0.03, file_path=target)
    saved = _spy_saves(monkeypatch)

    fake = MagicMock()
    fake.rewrite_buffer.return_value = AnimationResult("completed", 4)
    fake.resume.side_effect = [
        AnimationResult("interrupted", 1),  # one op landed, then S again
        AnimationResult("completed", 2),
    ]
    _install_fake_follower(monkeypatch, fake)

    with (
        # FollowerState.get gates update_current_file on the pane being alive
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", _scripted(["interrupt", "interrupt"])),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        hooks._await_user_handoff(current, _SESSION, target, after, "a\nb\nc\nd")

    first, second = fake.resume.call_args_list
    assert first.args[0].ops == _OPS
    assert second.args[0].ops == _OPS[1:]  # shortened by the one op replayed
    # the rebuilt partial is the previous one with that op applied
    assert fake.rewrite_buffer.call_args_list == [
        call(target, "a\nb\nc\nd"),
        call(target, "a\nB\nc\nd\n"),
    ]
    assert saved == [("apply_edit", _OPS[1:], 0.03, target)]
    assert fake.hand_over.call_count == 1
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file == target
    assert capsys.readouterr().out == ""


def test_a_save_after_the_second_interrupt_reports_the_accumulated_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The release path still works from a LATER cycle, and the notification
    # must quote everything shown so far — not just the first cycle's partial.
    after = "l0\nl1\nl2\nl3\n"
    current, target = _handoff_state(tmp_path, after)
    control.save_pending_show_fresh(
        "@1", ("l1", "l2", "l3"), 0.03, continuation=True, file_path=target
    )
    _spy_saves(monkeypatch)

    fake = MagicMock()
    fake.rewrite_buffer.return_value = AnimationResult("completed", 1)
    fake.resume.return_value = AnimationResult("interrupted", 2)
    _install_fake_follower(monkeypatch, fake)

    def _check(window_id: str, base_directory: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return "interrupt"
        Path(target).write_text("the user's own version\n")
        return None

    calls = {"n": 0}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", _check),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        hooks._await_user_handoff(current, _SESSION, target, after, "l0\n")

    out = json.loads(capsys.readouterr().out)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "l0\nl1\nl2\n" in context  # the grown partial, not just "l0"
    assert "SAVED their own version" in context
    assert not control.has_pending_animation("@1")  # the remainder is dropped on release


def test_a_mid_replay_interrupt_with_nothing_typed_keeps_the_remainder_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # completed_count == 0: the user hit S before the replay typed anything.
    # The remainder must come back byte-identical (and the empty partial keeps
    # the seed provenance, so the next replay still passes seeded=True).
    after = "l0\nl1\n"
    current, target = _handoff_state(tmp_path, after)
    control.save_pending_show_fresh("@1", ("l0", "l1"), 0.03, continuation=False, file_path=target)
    saved = _spy_saves(monkeypatch)

    fake = MagicMock()
    fake.rewrite_buffer.return_value = AnimationResult("completed", 0)
    fake.resume.side_effect = [
        AnimationResult("interrupted", 0),
        AnimationResult("completed", 2),
    ]
    _install_fake_follower(monkeypatch, fake)

    with (
        # FollowerState.get gates update_current_file on the pane being alive
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", _scripted(["interrupt", "interrupt"])),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        hooks._await_user_handoff(current, _SESSION, target, after, "")

    first, second = fake.resume.call_args_list
    assert second.args[0].lines == first.args[0].lines == ("l0", "l1")
    assert second.kwargs["seeded"] is True  # still nothing on screen: seed stands
    assert saved == [("show_fresh", ("l0", "l1"), 0.03, False)]
    assert fake.rewrite_buffer.call_args_list == [call(target, ""), call(target, "")]


@pytest.mark.integration
def test_two_replay_cycles_land_at_the_exact_content_in_a_real_nvim(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end through the real hook orchestration and a real Neovim: type,
    # interrupt, des-interrupt, interrupt the replay, des-interrupt again. The
    # buffer must end up holding EXACTLY the file's lines — no dropped line, no
    # leftover seed blank, no duplicated partial.
    pynvim = pytest.importorskip("pynvim")
    from vim_ai_follower.backends.nvim import NvimFollower

    target = tmp_path / "a.py"
    content = "l0\nl1\nl2\nl3\n"
    target.write_text(content)
    first_file = str(target)
    # speed=instant pins the pace to 0.0, so check_signal is called exactly
    # once per line and the script below is deterministic.
    state.FollowerState.set(
        "@1", "nvim", headless_nvim, open_files=(first_file,), shown_any=True, speed="instant"
    )
    current = state.FollowerState.read("@1")
    assert current is not None

    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    script: list[str | None] = [
        None,  # l0 types
        None,  # l1 types
        "interrupt",  # stop before l2
        "interrupt",  # hand-off poll: des-interrupt
        None,  # replay types l2
        "interrupt",  # stop mid-replay -> must RE-ARM, not return
        "interrupt",  # second hand-off poll: des-interrupt again
        None,  # replay types l3 -> completed
    ]
    monkeypatch.setattr(control, "check_signal", _scripted(script))
    monkeypatch.setattr(
        hooks, "status_surface_for", lambda *arguments, **keyword_arguments: MagicMock()
    )

    assert follower.show_fresh(first_file, content) == AnimationResult("interrupted", 2)
    partial = hooks._reconstruct_partial_fresh(content, 2)
    control.save_pending_show_fresh(
        "@1", ("l2", "l3"), 0.0, continuation=True, file_path=first_file
    )

    # Every poll here answers "interrupt" immediately, so no idle sleep is
    # expected — patching it turns a hypothetical hang into a fast failure.
    with patch("vim_ai_follower.hooks.time.sleep"):
        hooks._await_user_handoff(
            current,
            Session(window_id="@1", origin=None, in_tmux=False),
            first_file,
            content,
            partial,
        )

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["l0", "l1", "l2", "l3"]
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file == first_file


def test_the_s_that_ends_a_wait_marks_the_replay_running_before_it_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D1 residual: the wait consumed the first S and returned, but the marker
    kept "handoff" until the replay's driver started — a second S within
    those milliseconds was told "hand-off cancelled, unsaved changes
    discarded" though it interrupts the replay."""
    current, target = _handoff_state(tmp_path, "a\n")
    control.mark_animating("@1", state="handoff")
    assert control.request_interrupt("@1") is True  # the first S
    with patch("vim_ai_follower.hooks.time.sleep"):
        assert hooks._poll_until_interrupt(current, _SESSION, target, "a\n", "", None) is True
    assert control.animating_state("@1") == "running"

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0  # the second S
    assert capsys.readouterr().out == "claude-follow: interrupt requested\n"
    assert control.check_signal("@1") == "interrupt"  # addressed to the replay


@pytest.mark.parametrize("ending", ["saved", "replay completed"])
def test_a_hand_off_that_ends_reads_finishing_before_its_cue_comes_down(
    ending: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PF round-2 minor: after a save released the hand-off, the marker still
    read "handoff" while the cue was taken down, so an S there printed
    "hand-off cancelled, unsaved changes discarded" with nothing discarded."""
    current, target = _handoff_state(tmp_path, "a\n")
    fake = MagicMock()
    fake.rewrite_buffer.return_value = AnimationResult("completed", 0)
    fake.resume.return_value = AnimationResult("completed", 1)
    monkeypatch.setattr(hooks, "get_follower", lambda *arguments, **keyword_arguments: fake)
    monkeypatch.setattr(hooks, "show_popup", lambda *arguments, **keyword_arguments: None)
    control.save_pending_show_fresh("@1", ("a",), 0.0, file_path=target, partial="")
    control.mark_animating("@1")
    if ending == "saved":
        monkeypatch.setattr(
            hooks, "_poll_until_interrupt", lambda *arguments, **keyword_arguments: False
        )
    else:
        monkeypatch.setattr(hooks, "_poll_until_interrupt", _scripted_poll([True]))
    pressed: list[str] = []
    surface = MagicMock()

    def clear() -> None:
        with (
            patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
            patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
        ):
            assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
        pressed.append(capsys.readouterr().out)

    surface.clear.side_effect = clear
    hooks._await_user_handoff(current, _SESSION, target, "a\n", "", surface)
    assert pressed == ["claude-follow: nothing to interrupt — the animation is finishing\n"]
    assert control.check_signal("@1") is None


def _scripted_poll(answers: list[bool]) -> Callable[..., bool]:
    def poll(*_args: object, **_kwargs: object) -> bool:
        return answers.pop(0)

    return poll


def _signal_on_poll(nth: int) -> Callable[..., str | None]:
    """check_signal double: "interrupt" on the nth poll, None before it."""
    polls = {"count": 0}

    def _check(window_id: str, base_directory: Path | None = None) -> str | None:
        polls["count"] += 1
        return "interrupt" if polls["count"] == nth else None

    return _check


def test_a_crlf_handoff_waits_for_a_real_save(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Task 12 (CRLF files): the wait compared `read_text()` — universal
    newlines, every CR dropped — with `after`, decoded from the raw bytes
    with its CRs kept. A CRLF file then "differed" at the first poll, and
    the hook released Claude saying the user had saved their own version
    although nobody had saved anything."""
    after = "a\r\nX\r\nb\r\n"
    current, target = _handoff_state(tmp_path, "")
    Path(target).write_bytes(after.encode())
    initial_mtime = Path(target).stat().st_mtime_ns

    with (
        patch("vim_ai_follower.control.check_signal", _signal_on_poll(5)),
        patch("vim_ai_follower.hooks.time.sleep") as sleep,
    ):
        assert (
            hooks._poll_until_interrupt(current, _SESSION, target, after, "a\n", initial_mtime)
            is True
        )
    assert sleep.call_count == 4  # it kept polling until the S
    assert capsys.readouterr().out == ""  # and printed no release


def test_a_crlf_handoff_still_releases_on_a_save_of_other_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    after = "a\r\nX\r\nb\r\n"
    current, target = _handoff_state(tmp_path, "")
    Path(target).write_bytes(b"a\r\nUSER\r\nb\r\n")

    with (
        patch("vim_ai_follower.control.check_signal", _signal_on_poll(5)),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks._poll_until_interrupt(current, _SESSION, target, after, "a\n", None) is False
    assert "SAVED their own version" in capsys.readouterr().out


def test_a_save_that_changes_only_an_undecodable_byte_is_the_users_own_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T12+T13 review M1: the wait compared decoded text, and two different
    undecodable bytes both decode to U+FFFD, so a Latin-1 save that changed
    only such a byte released as "saved it unchanged — your version stands".
    It compares the file's raw bytes with the raw bytes Claude wrote."""
    raw_after = b"caf\xe9\nX\n"
    current, target = _handoff_state(tmp_path, "")
    Path(target).write_bytes(raw_after)
    initial_mtime = Path(target).stat().st_mtime_ns
    after = raw_after.decode("utf-8", errors="replace")
    Path(target).write_bytes(b"caf\xe8\nX\n")  # the user's save
    os.utime(target, ns=(initial_mtime + 10**9, initial_mtime + 10**9))

    with (
        patch("vim_ai_follower.control.check_signal", _signal_on_poll(5)),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert (
            hooks._poll_until_interrupt(
                current, _SESSION, target, after, "caf�\n", initial_mtime, raw_after=raw_after
            )
            is False
        )
    assert "SAVED their own version" in capsys.readouterr().out


def _stop_on_sleep(nth: int, stop: Callable[[], None]) -> Callable[[float], None]:
    """time.sleep double for the hand-off poll: runs `stop` (what
    `claude-follow stop` does to the window) at the nth idle sleep, and fails
    loudly when the wait outlives the budget instead of hanging (CI has no
    pytest-timeout)."""
    sleeps = {"count": 0}

    def _sleep(_seconds: float) -> None:
        sleeps["count"] += 1
        if sleeps["count"] == nth:
            stop()
        if sleeps["count"] > nth + 50:
            raise AssertionError("the hand-off wait never noticed the stop")

    return _sleep


def _stop_by_clearing_the_state() -> None:
    # What `claude-follow stop` does, in its order: the tombstone first.
    state.mark_stopped("@1", None, in_tmux=False)
    state.FollowerState.clear("@1")
    control.clear_signals("@1")
    control.discard_pending_animation("@1")


def _stop_by_tombstone_only() -> None:
    # The first thing `stop` writes; the state is cleared right after it.
    state.mark_stopped("@1", None, in_tmux=False)


def _restart_under_another_follower() -> None:
    state.FollowerState.clear("@1")
    _register_fake_follower("@1", "%9")


@pytest.mark.parametrize(
    "stop",
    [_stop_by_clearing_the_state, _stop_by_tombstone_only, _restart_under_another_follower],
    ids=["state cleared", "tombstone", "another follower"],
)
def test_a_stop_during_the_hand_off_wait_releases_claude(
    stop: Callable[[], None], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """T20 review m3: the poll never noticed `claude-follow stop`, so Claude
    stayed held until the hook's 600 s timeout. It now ends the wait with a
    message that the follower was stopped, and leaves no remainder or
    signal behind."""
    current, target = _handoff_state(tmp_path, "a\nb\n")
    control.save_pending_apply_edit("@1", list(_OPS), 0.0, file_path=target, partial="a\n")
    with (
        patch("vim_ai_follower.control.check_signal", _scripted([])),
        patch("vim_ai_follower.hooks.time.sleep", _stop_on_sleep(3, stop)),
    ):
        assert (
            hooks._poll_until_interrupt(current, _SESSION, target, "a\nb\n", "a\n", None) is False
        )
    message = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "stopped" in message and target in message
    assert not control.has_pending_animation("@1")
    assert control.check_signal("@1") is None


def test_a_state_another_windows_stop_swept_does_not_end_the_hand_off_wait(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final review I4: another window's `stop` sweeps every `.pane` whose
    editor looks dead (a dedicated Vim running `:!pytest` does), with no
    tombstone for this window. A missing state alone is no stop: the wait
    goes on, and the user's save still releases Claude with their version."""
    current, target = _handoff_state(tmp_path, "a\nb\n")

    polls = {"count": 0}

    def swept_then_saved(_seconds: float) -> None:
        polls["count"] += 1
        if polls["count"] == 2:
            state.FollowerState.clear("@1")  # the other window's sweep
        if polls["count"] == 6:
            Path(target).write_text("a\nUSER\n")  # the user's :w
        if polls["count"] > 60:
            raise AssertionError("the hand-off wait never saw the save")

    with (
        patch("vim_ai_follower.control.check_signal", _scripted([])),
        patch("vim_ai_follower.hooks.time.sleep", swept_then_saved),
    ):
        assert (
            hooks._poll_until_interrupt(current, _SESSION, target, "a\nb\n", "a\n", None) is False
        )
    message = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "SAVED their own version" in message
    assert "stopped" not in message


@pytest.mark.parametrize("content", ["null", "[]", "{}"])
def test_a_state_of_the_wrong_json_shape_does_not_crash_the_stop_test(
    content: str, tmp_path: Path
) -> None:
    """Final review M13: FollowerState.read raises TypeError (or KeyError) on
    a `.pane` holding `null` or a list. It escaped the hand-off poll, run_hook
    swallowed it, and Claude was released with no notification. Such a file
    proves nothing; only the tombstone decides."""
    current, _target = _handoff_state(tmp_path, "a\n")
    state._state_path("@1", None).write_text(content)
    assert hooks._follower_stopped(current, _SESSION) is False
    state.mark_stopped("@1", None, in_tmux=False)
    assert hooks._follower_stopped(current, _SESSION) is True


def test_a_state_caught_mid_write_does_not_end_the_hand_off_wait(tmp_path: Path) -> None:
    # A state file read while another hook rewrites it proves nothing about
    # a stop: the wait goes on, and the next poll reads it whole.
    current, _target = _handoff_state(tmp_path, "a\n")
    state_file = state._state_path("@1", None)
    whole = state_file.read_text()
    state_file.write_text(whole[: len(whole) // 2])
    assert hooks._follower_stopped(current, _SESSION) is False
    state_file.write_text(whole)
    assert hooks._follower_stopped(current, _SESSION) is False


def test_a_hand_off_the_stop_ended_leaves_the_surface_to_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `stop` takes the follower's cues down itself (or, for an adopted editor
    # whose user is typing, deliberately sends nothing): the hook restoring
    # its saved border title after the stop would put a cue back.
    current, target = _handoff_state(tmp_path, "a\n")
    monkeypatch.setattr(hooks, "get_follower", lambda *arguments, **keyword_arguments: MagicMock())
    surface = MagicMock()
    with (
        patch("vim_ai_follower.control.check_signal", _scripted([])),
        patch("vim_ai_follower.hooks.time.sleep", _stop_on_sleep(2, _stop_by_clearing_the_state)),
    ):
        hooks._await_user_handoff(current, _SESSION, target, "a\n", "", surface)
    surface.clear.assert_not_called()
    assert "stopped" in capsys.readouterr().out


def test_a_catch_up_hand_off_the_stop_ended_releases_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    current, target = _handoff_state(tmp_path, "a\n")
    follower = MagicMock()
    monkeypatch.setattr(hooks, "get_follower", lambda *arguments, **keyword_arguments: follower)
    monkeypatch.setattr(
        hooks, "status_surface_for", lambda *arguments, **keyword_arguments: MagicMock()
    )
    with (
        patch("vim_ai_follower.control.check_signal", _scripted([])),
        patch("vim_ai_follower.hooks.time.sleep", _stop_on_sleep(2, _stop_by_clearing_the_state)),
    ):
        assert (
            hooks._hand_over_interrupted_catch_up(
                current, _SESSION, follower, target, b"a\n", "a\n"
            )
            == 0
        )
    assert "stopped" in capsys.readouterr().out
    assert not control.has_pending_animation("@1")


def test_a_replay_a_stop_ended_while_paused_releases_claude_with_no_remainder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Final review C1: S, the replay starts, the user pauses it and runs
    # `claude-follow stop`. The replay's pause wait ends "stopped": nothing
    # is re-armed and Claude is released at once with its version standing.
    after = "l0\nl1\nl2\n"
    current, target = _handoff_state(tmp_path, after)
    control.save_pending_show_fresh("@1", ("l1", "l2"), 0.03, continuation=True, file_path=target)
    fake = MagicMock()
    fake.resume.return_value = AnimationResult("stopped", 0)
    _install_fake_follower(monkeypatch, fake)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", _scripted(["interrupt"])),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        hooks._await_user_handoff(current, _SESSION, target, after, "l0\n")
    assert fake.resume.call_count == 1
    assert control.has_pending_animation("@1") is False
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "paused the live preview" in context
    assert "stopped the follower" in context
