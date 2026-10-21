"""After a hook's LAST animation returns it still holds the window's slot
(for its bookkeeping: cue refresh, current-file pointer), so the marker must
not read "running" there: a P or S pressed in that window used to be
answered "pause requested" / "interrupt requested", addressed to the hook
and never consumed. The hook marks "finishing", which cmd_pause and
cmd_interrupt treat as nothing to act on."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from helpers import make_mock_tmux_run
from test_hooks_base_mismatch import (
    _caught_up_follower,
    _follower,
    _pending_apply_edit,
    _run_hook,
    _setup,
)

from vim_ai_follower import control, hooks, state, tmux
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends import NavigationFailed


def _state_at_release(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """What the marker says at the moment the hook releases its slot."""
    seen: list[str | None] = []
    real: Callable[..., None] = control.clear_animating

    def release(window_id: str, base_directory: Path | None = None) -> None:
        seen.append(control.animating_state(window_id, base_directory))
        real(window_id, base_directory)

    monkeypatch.setattr(control, "clear_animating", release)
    return seen


@pytest.mark.parametrize("outcome", ["diff", "retype", "interrupted then released"])
def test_the_hook_marks_finishing_once_its_last_animation_returns(
    outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, _resolved = _setup(tmp_path)
    follower = _follower("absent" if outcome == "retype" else "holds")
    if outcome == "interrupted then released":
        follower.apply_edit.return_value = AnimationResult("interrupted", 0)
        monkeypatch.setattr(
            hooks, "_await_user_handoff", lambda *arguments, **keyword_arguments: None
        )
    seen = _state_at_release(monkeypatch)
    _run_hook(target, follower, monkeypatch)
    assert seen == ["finishing"]
    assert control.is_animating("@1") is False


# --- a P or S that no driver consumed (final M6, PF round 2) ----------------


def _press(signal: str, answer: object = None) -> Callable[..., object]:
    """A follower call during which the user presses P or S: the CLI addresses
    the signal to the slot's owner (this test process stands in for the hook)."""

    def _call(*_args: object, **_kwargs: object) -> object:
        assert control.animating_state("@1") == "running"
        if signal == "pause":
            assert control.request_pause("@1") == 1
        else:
            assert control.request_interrupt("@1") is True
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return _call


def _no_signal_left(tmp_path: Path) -> None:
    assert control.check_signal("@1") is None
    from vim_ai_follower import cache

    assert sorted(path.name for path in cache.CACHE_DIRECTORY.glob("@1.pause*")) == []
    assert sorted(path.name for path in cache.CACHE_DIRECTORY.glob("@1.interrupt*")) == []


@pytest.mark.parametrize(
    "path", ["leave-alone", "binary", "navigation-failed", "after-the-last-check"]
)
def test_a_pause_no_driver_consumed_is_drained_and_its_paused_cue_retired(
    path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook held the slot as "running", so `claude-follow pause` said
    "pause requested" and put "Paused" on the nvim float — but no driver ever
    read the signal. It used to stay on disk (dropped as stale by the NEXT
    hook) with "Paused" left on screen."""
    target, _resolved = _setup(tmp_path, adopted=path == "leave-alone")
    follower = _follower("holds")
    if path == "leave-alone":
        follower.probe_buffer.side_effect = _press("pause", "differs")
    elif path == "binary":
        target.write_bytes(b"\x00\x01binary")
        follower.ensure_showing.side_effect = _press("pause")
    elif path == "navigation-failed":
        follower.apply_edit.side_effect = _press("pause", NavigationFailed("did not land"))
    else:  # pressed after the driver's last signal check, before "finishing"
        follower.apply_edit.side_effect = _press("pause", AnimationResult("completed", 1))
    surface = _run_hook(target, follower, monkeypatch)

    assert call.retire_state("Paused") in surface.method_calls
    _no_signal_left(tmp_path)
    assert control.is_animating("@1") is False


def test_a_hook_with_nothing_left_for_it_touches_no_cue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, _resolved = _setup(tmp_path)
    surface = _run_hook(target, _follower("holds"), monkeypatch)
    assert call.retire_state("Paused") not in surface.method_calls


def test_the_drain_marks_finishing_before_it_reads_the_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So cmd_pause, re-reading the state after its own feedback, can tell
    the drain may already have run (and take its "Paused" back itself)."""
    target, _resolved = _setup(tmp_path)
    follower = _follower("holds")
    follower.apply_edit.side_effect = _press("pause", NavigationFailed("did not land"))
    seen: list[str | None] = []
    real_check = control.check_signal

    def check(window_id: str, base_directory: Path | None = None) -> str | None:
        seen.append(control.animating_state(window_id, base_directory))
        return real_check(window_id, base_directory)

    monkeypatch.setattr(control, "check_signal", check)
    _run_hook(target, follower, monkeypatch)
    assert seen and set(seen) == {"finishing"}


@pytest.mark.parametrize(
    ("signal", "message"), [("pause", "Nothing to pause"), ("interrupt", "Nothing to interrupt")]
)
def test_on_tmux_a_drained_press_is_answered_once_its_own_popup_closed(
    signal: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tmux drops a popup opened while another is up (measured, tmux 3.7c),
    so the correction waits out the press's own "Paused"/"Interrupted"."""
    control.mark_animating("@1")
    if signal == "pause":
        control.request_pause("@1")
    else:
        control.request_interrupt("@1")
    popups: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        hooks,
        "show_popup",
        lambda *arguments, **keyword_arguments: popups.append((arguments, keyword_arguments)),
    )
    current = state.FollowerState(
        backend="tmux",
        target="%2",
        current_file=None,
        origin="%1",
        on_failure="log",
        speed="normal",
    )
    hooks.drain_unconsumed_signals("@1", current, True)
    [(arguments, keyword_arguments)] = popups
    assert isinstance(keyword_arguments, dict)
    assert arguments == ("%2", message)
    assert keyword_arguments["after_seconds"] == tmux.POPUP_SECONDS + 0.1
    token = keyword_arguments["withdraw_token"]
    assert isinstance(token, Path) and token.exists()  # a newer press can withdraw it
    hooks.drain_unconsumed_signals("@1", current, False)  # nothing left: no second answer
    assert len(popups) == 1


def test_a_p_and_an_s_drained_together_show_one_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sweep-5 T22: two corrections opened at the same moment, and tmux showed
    only the first, so "Nothing to interrupt" was lost behind "Nothing to
    pause". One popup says both."""
    control.mark_animating("@1")
    control.request_pause("@1")
    control.request_interrupt("@1")
    popups: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        hooks,
        "show_popup",
        lambda *arguments, **keyword_arguments: popups.append(arguments),
    )
    current = state.FollowerState(
        backend="tmux",
        target="%2",
        current_file=None,
        origin="%1",
        on_failure="log",
        speed="normal",
    )
    hooks.drain_unconsumed_signals("@1", current, True)
    assert popups == [("%2", "Nothing to pause or interrupt")]


def test_on_nvim_a_p_and_an_s_drained_together_take_both_cues_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control.mark_animating("@1")
    control.request_pause("@1")
    control.request_interrupt("@1")
    surface = MagicMock()
    monkeypatch.setattr(hooks, "status_surface_for", lambda _current: surface)
    current = state.FollowerState(
        backend="nvim",
        target="/tmp/n.sock",
        current_file=None,
        origin="%1",
        on_failure="log",
        speed="normal",
    )
    hooks.drain_unconsumed_signals("@1", current, True)
    assert surface.method_calls == [call.retire_state("Interrupted"), call.retire_state("Paused")]


def test_a_drained_press_outside_tmux_or_without_a_follower_shows_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popups: list[object] = []
    monkeypatch.setattr(
        hooks, "show_popup", lambda *arguments, **keyword_arguments: popups.append(arguments)
    )
    tmux_state = state.FollowerState(
        backend="tmux",
        target="%2",
        current_file=None,
        origin="%1",
        on_failure="log",
        speed="normal",
    )
    for current, in_tmux in ((tmux_state, False), (None, True)):
        control.mark_animating("@1")
        control.request_pause("@1")
        hooks.drain_unconsumed_signals("@1", current, in_tmux)
        assert control.check_signal("@1") is None
    assert popups == []


def test_a_drain_that_fails_never_fails_the_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, _resolved = _setup(tmp_path)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError("cache dir gone")

    monkeypatch.setattr(control, "check_signal", broken)
    _run_hook(target, _follower("holds"), monkeypatch)
    assert control.is_animating("@1") is False
    assert "could not drain" in hooks.LOG_PATH.read_text()


def test_a_press_addressed_to_a_read_is_drained_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Read holds the slot as "finishing", so a fresh press is told there is
    nothing to pause. But one that read "running" from the hook before it
    and wrote its signal after the Read took the slot is addressed to the
    Read, which runs no driver."""
    target, _resolved = _setup(tmp_path)
    follower = _follower("holds")

    def navigate(*_args: object, **_kwargs: object) -> None:
        assert control.request_pause("@1") == 1

    follower.ensure_showing.side_effect = navigate
    surface = MagicMock()
    monkeypatch.setattr(hooks, "get_follower", lambda *arguments, **keyword_arguments: follower)
    monkeypatch.setattr(
        hooks, "status_surface_for", lambda *arguments, **keyword_arguments: surface
    )
    payload: dict[str, object] = {"tool_name": "Read", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0
    assert follower.ensure_showing.call_args_list != []
    assert call.retire_state("Paused") in surface.method_calls
    _no_signal_left(tmp_path)


# --- the marker between an interrupted driver and the hand-off wait ---------


@pytest.mark.parametrize("animation", ["diff", "retype"])
def test_an_interrupted_animation_reads_handoff_before_its_remainder_is_saved(
    animation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slot residual: from the driver's return to the wait's first mark the
    marker still said "running", so an S there was told "interrupt requested"
    instead of being the des-interrupt it is."""
    target, _resolved = _setup(tmp_path)
    follower = _follower("absent" if animation == "retype" else "holds")
    follower.apply_edit.return_value = AnimationResult("interrupted", 0)
    follower.show_fresh.return_value = AnimationResult("interrupted", 1)
    monkeypatch.setattr(hooks, "_await_user_handoff", lambda *arguments, **keyword_arguments: None)
    seen: list[str | None] = []
    for name in ("save_pending_apply_edit", "save_pending_show_fresh"):
        real = getattr(control, name)

        def save(
            *arguments: object, _real: Callable[..., None] = real, **keyword_arguments: object
        ) -> None:
            seen.append(control.animating_state("@1"))
            _real(*arguments, **keyword_arguments)

        monkeypatch.setattr(control, name, save)
    _run_hook(target, follower, monkeypatch)
    assert seen == ["handoff"]


def test_an_interrupted_replay_reads_handoff_only_after_hand_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same window inside the hand-off loop, ordered the other way (T20
    re-review m5): a replay interrupted by S returns with the marker
    "running" (its driver's), and the re-arm saves the shorter remainder and
    sends hand_over's lines before it reads "handoff", since a user who took
    "handoff" as theirs typed into those lines. An S meanwhile is still
    consumed as the des-interrupt (test_hooks_catchup_partial's
    `…keeps_an_s_sent_meanwhile`); only its popup says "Interrupted"."""
    target = tmp_path / "f.txt"
    target.write_text("l0\nl1\n")
    control.save_pending_show_fresh("@1", ("l1",), 0.0, continuation=True, file_path=str(target))
    control.mark_animating("@1")  # the replay's driver left it "running"
    follower = MagicMock()
    seen: list[str | None] = []
    real = control.save_pending_show_fresh

    def save(*arguments: object, **keyword_arguments: object) -> None:
        seen.append(control.animating_state("@1"))
        real(*arguments, **keyword_arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(control, "save_pending_show_fresh", save)
    pending = control.load_pending_animation("@1")
    assert pending is not None
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()):
        hooks._rearm_handoff(follower, "@1", pending, "l0\n", 0)
    assert seen == ["running"]
    assert control.animating_state("@1") == "handoff"


@pytest.mark.parametrize(
    "presses",
    [("pause", "interrupt"), ("interrupt", "pause"), ("pause", "pause")],
    ids=["P-then-S", "S-then-P", "P-twice"],
)
def test_every_press_no_driver_consumed_is_drained(
    presses: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review I1: one check_signal returns at the first file it consumes
    (interrupt before pause), so a P left next to an S stayed on disk with
    "Paused" on the float."""
    target, _resolved = _setup(tmp_path)
    follower = _follower("holds")

    def both(*_args: object, **_kwargs: object) -> None:
        for signal in presses:
            requested = (
                control.request_pause("@1")
                if signal == "pause"
                else control.request_interrupt("@1")
            )
            assert requested
        raise NavigationFailed("did not land")

    follower.apply_edit.side_effect = both
    surface = _run_hook(target, follower, monkeypatch)

    retired = [entry for entry in surface.method_calls if entry[0] == "retire_state"]
    if presses == ("pause", "pause"):
        # A pause and its resume (T20 m4): the second press's own cue already
        # said "Resuming", so there is no "Paused" to take back.
        assert call.retire_state("Paused") not in retired
    else:
        assert call.retire_state("Paused") in retired
    if "interrupt" in presses:
        assert call.retire_state("Interrupted") in retired
    _no_signal_left(tmp_path)


def test_a_pause_pressed_during_a_grounding_reload_is_drained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of T11, m4: the grounding exit (a completed catch-up whose
    buffer is not the edit's base is reloaded from disk) runs no driver
    after the catch-up, so a P pressed during the reload is the drain's."""
    target, resolved = _setup(tmp_path)
    _pending_apply_edit(resolved, "a\nb\nc\n")
    follower = _caught_up_follower(False, "differs")
    follower.reload_from_disk.side_effect = _press("pause")
    surface = _run_hook(target, follower, monkeypatch)

    assert follower.reload_from_disk.call_args_list != []
    assert follower.apply_edit.call_args_list == []
    assert call.retire_state("Paused") in surface.method_calls
    _no_signal_left(tmp_path)


@pytest.mark.parametrize("outcome", ["diff", "retype"])
def test_an_interrupted_animation_hands_the_wait_the_bytes_claude_wrote(
    outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T12+T13 review M1: the hand-off wait compares the file's raw bytes with
    these, never decoded text (two undecodable bytes decode alike)."""
    target, _resolved = _setup(tmp_path)
    target.write_bytes(b"caf\xe9\r\nX\r\n")
    follower = _follower("absent" if outcome == "retype" else "holds")
    follower.apply_edit.return_value = AnimationResult("interrupted", 0)
    follower.show_fresh.return_value = AnimationResult("interrupted", 0)
    handed: list[object] = []
    monkeypatch.setattr(
        hooks,
        "_await_user_handoff",
        lambda *arguments, **keyword_arguments: handed.append(keyword_arguments.get("raw_after")),
    )
    _run_hook(target, follower, monkeypatch)
    assert handed == [b"caf\xe9\r\nX\r\n"]


@pytest.mark.parametrize("animation", ["diff", "retype", "catch-up"])
def test_an_animation_a_stop_ended_while_paused_releases_claude_at_once(
    animation: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Final review C1: the driver's pause wait saw `claude-follow stop` and
    ended "stopped". No hand-off, no remainder, no further driver call; the
    marker reads finishing and Claude is told its version stands."""
    target, resolved = _setup(tmp_path)
    follower = _follower("absent" if animation == "retype" else "holds")
    follower.apply_edit.return_value = AnimationResult("stopped", 0)
    follower.show_fresh.return_value = AnimationResult("stopped", 1)
    if animation == "catch-up":
        _pending_apply_edit(resolved, "a\nb\nc\n")
        follower.resume.return_value = AnimationResult("stopped", 0)
    handoffs: list[object] = []
    monkeypatch.setattr(
        hooks, "_await_user_handoff", lambda *arguments, **keyword_arguments: handoffs.append(1)
    )
    seen = _state_at_release(monkeypatch)
    _run_hook(target, follower, monkeypatch)
    assert handoffs == []
    assert seen == ["finishing"]
    assert control.has_pending_animation("@1") is False
    if animation == "catch-up":
        follower.apply_edit.assert_not_called()
        follower.show_fresh.assert_not_called()
    assert "paused the live preview" in capsys.readouterr().out
