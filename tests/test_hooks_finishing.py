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
from test_hooks_base_mismatch import _follower, _run_hook, _setup

from vim_ai_follower import control, hooks, state, tmux
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends import NavigationFailed


def _state_at_release(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """What the marker says at the moment the hook releases its slot."""
    seen: list[str | None] = []
    real: Callable[..., None] = control.clear_animating

    def release(window_id: str, base_dir: Path | None = None) -> None:
        seen.append(control.animating_state(window_id, base_dir))
        real(window_id, base_dir)

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
        monkeypatch.setattr(hooks, "_await_user_handoff", lambda *a, **k: None)
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
            assert control.request_pause("@1") is True
        else:
            assert control.request_interrupt("@1") is True
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return _call


def _no_signal_left(tmp_path: Path) -> None:
    assert control.check_signal("@1") is None
    from vim_ai_follower import cache

    assert sorted(path.name for path in cache.CACHE_DIR.glob("@1.pause*")) == []
    assert sorted(path.name for path in cache.CACHE_DIR.glob("@1.interrupt*")) == []


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

    def check(window_id: str, base_dir: Path | None = None) -> str | None:
        seen.append(control.animating_state(window_id, base_dir))
        return real_check(window_id, base_dir)

    monkeypatch.setattr(control, "check_signal", check)
    _run_hook(target, follower, monkeypatch)
    assert seen == ["finishing"]


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
    monkeypatch.setattr(hooks, "show_popup", lambda *args, **kwargs: popups.append((args, kwargs)))
    current = state.FollowerState(
        backend="tmux",
        target="%2",
        current_file=None,
        origin="%1",
        on_failure="log",
        speed="normal",
    )
    hooks._drain_unconsumed_signals("@1", current, True)
    assert popups == [(("%2", message), {"after_seconds": tmux.POPUP_SECONDS + 0.1})]
    hooks._drain_unconsumed_signals("@1", current, False)  # nothing left: no second answer
    assert len(popups) == 1


def test_a_drained_press_outside_tmux_or_without_a_follower_shows_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popups: list[object] = []
    monkeypatch.setattr(hooks, "show_popup", lambda *args, **kwargs: popups.append(args))
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
        hooks._drain_unconsumed_signals("@1", current, in_tmux)
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
        assert control.request_pause("@1") is True

    follower.ensure_showing.side_effect = navigate
    surface = MagicMock()
    monkeypatch.setattr(hooks, "get_follower", lambda *args, **kwargs: follower)
    monkeypatch.setattr(hooks, "status_surface_for", lambda *args, **kwargs: surface)
    payload: dict[str, object] = {"tool_name": "Read", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
        patch("pynvim.attach", return_value=MagicMock()),
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
    monkeypatch.setattr(hooks, "_await_user_handoff", lambda *args, **kwargs: None)
    seen: list[str | None] = []
    for name in ("save_pending_apply_edit", "save_pending_show_fresh"):
        real = getattr(control, name)

        def save(*args: object, _real: Callable[..., None] = real, **kwargs: object) -> None:
            seen.append(control.animating_state("@1"))
            _real(*args, **kwargs)

        monkeypatch.setattr(control, name, save)
    _run_hook(target, follower, monkeypatch)
    assert seen == ["handoff"]


def test_an_interrupted_replay_reads_handoff_before_its_remainder_is_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same window inside the hand-off loop: a replay interrupted by S
    returns with the marker "running" (its driver's), and the re-arm saves
    the shorter remainder and hands the buffer over before the wait marks."""
    target = tmp_path / "f.txt"
    target.write_text("l0\nl1\n")
    control.save_pending_show_fresh("@1", ("l1",), 0.0, continuation=True, file_path=str(target))
    control.mark_animating("@1")  # the replay's driver left it "running"
    follower = MagicMock()
    seen: list[str | None] = []
    real = control.save_pending_show_fresh

    def save(*args: object, **kwargs: object) -> None:
        seen.append(control.animating_state("@1"))
        real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(control, "save_pending_show_fresh", save)
    pending = control.load_pending_animation("@1")
    assert pending is not None
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()):
        hooks._rearm_handoff(follower, "@1", pending, "l0\n", 0)
    assert seen == ["handoff"]
