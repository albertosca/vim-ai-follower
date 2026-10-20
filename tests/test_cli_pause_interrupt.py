from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from helpers import call_spelled, resolve_typed_paths
from helpers import make_mock_tmux_run as _mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import cache, cli, commands, control, session, state, tmux
from vim_ai_follower.backends import NavigationFailed
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import EditOp


def _popup_calls(popen_mock: MagicMock) -> list[list[str]]:
    return [
        recorded_call.args[0]
        for recorded_call in popen_mock.call_args_list
        if recorded_call.args[0][:2] == ["tmux", "display-popup"]
    ]


def test_pause_dead_tmux_pane_fails() -> None:
    # {} alone no longer means "fail" — no TMUX_PANE now resolves to the
    # standalone session. A dead-pane resolve_session() -> None is the case
    # that must still fail, mirroring the old _require_window(env) is None.
    with patch("vim_ai_follower.commands.resolve_session", return_value=None):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 1


def test_interrupt_dead_tmux_pane_fails() -> None:
    with patch("vim_ai_follower.commands.resolve_session", return_value=None):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 1


def test_pause_standalone_writes_signal_but_skips_the_tmux_popup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Standalone (no TMUX_PANE): resolve_session() returns in_tmux=False. A
    # pause press must still request the pause (the control signal keys on
    # window_id and already works standalone) but must never shell out to
    # tmux display-popup — there is no tmux server to target. A "tmux"
    # backend is registered under the standalone id on purpose, to prove the
    # skip is driven by session.in_tmux and not merely by current being None
    # or the backend being nvim.
    _register_fake_follower("term-x", "%2")
    standalone_session = session.Session(window_id="term-x", origin=None, in_tmux=False)
    control.mark_animating("term-x")
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=standalone_session),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_pause({}) == 0
    assert control.check_signal("term-x") == "pause"
    assert "pause requested" in capsys.readouterr().out
    assert _popup_calls(popen) == []


def test_pause_requests_pause_while_an_animation_is_running(
    capsys: pytest.CaptureFixture[str],
) -> None:
    control.mark_animating("@1")  # this test process stands in for the hook
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    assert control.check_signal("@1") == "pause"
    assert "pause requested" in capsys.readouterr().out


def test_interrupt_requests_interrupt_while_an_animation_is_running(
    capsys: pytest.CaptureFixture[str],
) -> None:
    control.mark_animating("@1")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
    assert control.check_signal("@1") == "interrupt"
    assert "interrupt requested" in capsys.readouterr().out


def test_pause_shows_a_popup_on_the_follower_pane_when_requesting_pause() -> None:
    _register_fake_follower("@1", "%2")
    control.mark_animating("@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    popups = _popup_calls(popen)
    assert len(popups) == 1
    assert popups[0][:3] == ["tmux", "display-popup", "-t"]
    assert popups[0][3] == "%2"
    assert "-E" in popups[0]
    assert any("Paused" in argument for argument in popups[0])


def test_interrupt_shows_a_popup_on_the_follower_pane() -> None:
    _register_fake_follower("@1", "%2")
    control.mark_animating("@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
    popups = _popup_calls(popen)
    assert len(popups) == 1
    assert popups[0][3] == "%2"
    assert any("Interrupted" in argument for argument in popups[0])


def test_pause_skips_popup_when_no_follower_is_registered() -> None:
    control.mark_animating("@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    assert _popup_calls(popen) == []


def test_interrupt_skips_popup_when_no_follower_is_registered() -> None:
    control.mark_animating("@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
    assert _popup_calls(popen) == []


def test_pause_shows_paused_in_the_nvim_surface_not_a_tmux_popup() -> None:
    # nvim has no pane to popup on (its target is an RPC socket), so a pause
    # renders "Paused" in the floating status surface instead. pynvim.attach is
    # stubbed so FollowerState.get's is_alive() liveness check passes (a dead
    # socket would drop the follower and never reach the feedback).
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock")
    control.mark_animating("@1")
    surface = MagicMock()
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.status_surface_for", return_value=surface),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    surface.set_state.assert_called_once_with("Paused")
    assert _popup_calls(popen) == []


def test_pause_resume_restores_writing_on_the_nvim_surface_not_a_tmux_popup() -> None:
    # Resuming an nvim follower restores the "Writing..." activity line via the
    # surface (keeping a writer cue's title/border), with no tmux popup. Same
    # is_alive() stubbing as the pause case.
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock")
    control.mark_animating("@1", state="paused")
    surface = MagicMock()
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.status_surface_for", return_value=surface),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    surface.set_state.assert_called_once_with("Writing...")
    assert _popup_calls(popen) == []


def test_pause_discards_a_crash_orphaned_nvim_pending_without_crashing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A paused nvim animation whose hook was KILLED leaves a pending on disk
    # with a stale (dead-PID) animating marker. A later `prefix P` must not hit
    # the tmux-only keyboard-replay resume (which would AssertionError); it
    # discards the orphaned pending and reports gracefully.
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock")
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [operation], 0.0)  # crash fallback, no live marker

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
        # get() -> is_alive() must see the adopted nvim as alive.
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0

    assert control.has_pending_animation("@1") is False
    assert "nothing to resume" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (commands.cmd_pause, "claude-follow: nothing to pause — the animation is finishing\n"),
        (
            commands.cmd_interrupt,
            "claude-follow: nothing to interrupt — the animation is finishing\n",
        ),
    ],
)
def test_a_press_while_the_hook_is_finishing_is_nothing_to_act_on(
    command: object, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # The hook's last animation has returned; it still holds the slot for
    # its bookkeeping. A signal now would be addressed to it and never
    # consumed, and a "Paused"/"Interrupted" popup would lie.
    _register_fake_follower("@1", "%2")
    control.mark_animating("@1", state="finishing")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert command({"TMUX_PANE": "%1"}) == 0  # type: ignore[operator]
    assert capsys.readouterr().out == message
    assert _popup_calls(popen) == []
    assert control.check_signal("@1") is None  # nothing was written
    assert control.animating_state("@1") == "finishing"


def test_pause_resumes_pending_apply_edit(capsys: pytest.CaptureFixture[str]) -> None:
    _register_fake_follower("@1", "%2")
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [operation], 0.0)

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0

    sends = [
        recorded_call.args[0][6]
        for recorded_call in run.call_args_list
        if recorded_call.args[0][:4] == ["tmux", "send-keys", "-t", "%2"]
        and "-l" in recorded_call.args[0]
    ]
    assert "resumed" in sends
    assert control.has_pending_animation("@1") is False
    assert "resumed (completed)" in capsys.readouterr().out


def test_a_keyboard_resume_a_stop_ends_while_paused_again_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Final review C1: P resumes the remainder from the CLI, P pauses the
    # replay again, then `claude-follow stop`. The replay's pause wait ends
    # "stopped": no remainder kept, no popup to the stopped follower's pane.
    _register_fake_follower("@1", "%2")
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [operation], 0.0)
    checks = {"count": 0}

    def _check(window_id: str, base_directory: object = None) -> str | None:
        checks["count"] += 1
        if checks["count"] == 1:
            return "pause"
        if checks["count"] == 2:
            state.mark_stopped("@1", None, in_tmux=False)
        return None

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
        patch("vim_ai_follower.control.check_signal", side_effect=_check),
        patch("vim_ai_follower.animate.time.sleep"),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    assert capsys.readouterr().out == "claude-follow: resume ended by `claude-follow stop`\n"
    assert len(_popup_calls(popen)) == 1  # "Resuming" only
    assert control.has_pending_animation("@1") is False
    assert control.is_animating("@1") is False


def test_pause_resume_owns_the_slot_while_it_replays_and_releases_it() -> None:
    # The keyboard resume animates from the CLI process: it must hold the
    # window's slot like a hook, or a hook arriving mid-replay animates too.
    _register_fake_follower("@1", "%2")
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [operation], 0.0)
    seen: list[str | None] = []

    def _check(window_id: str, base_directory: object = None) -> None:
        seen.append(control.animating_state("@1"))

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
        patch("vim_ai_follower.control.check_signal", side_effect=_check),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    # The replay's checks, then the exit drain's (final m3), "finishing".
    assert len(seen) > 1 and set(seen[:-1]) == {"running"} and seen[-1] == "finishing"
    assert control.is_animating("@1") is False  # released on the way out


def test_pause_resume_skips_when_a_hook_owns_the_slot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The slot is taken BEFORE the remainder is loaded: loading consumes the
    # file, so a refusal after the load had to put it back, racing the hook
    # that holds the slot (it consumes the same file as its catch-up).
    _register_fake_follower("@1", "%2")
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [operation], 0.0)
    pending_at_acquire: list[bool] = []

    def held_by_a_hook(window_id: str, *_args: object, **_kwargs: object) -> bool:
        pending_at_acquire.append(control.has_pending_animation(window_id))
        return False

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
        patch("vim_ai_follower.commands.control.try_acquire_animating", side_effect=held_by_a_hook),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 1
    assert pending_at_acquire == [True], "the remainder was consumed before the slot was taken"
    assert not [
        recorded_call
        for recorded_call in run.call_args_list
        if recorded_call.args[0][:2] == ["tmux", "send-keys"]
    ]
    assert capsys.readouterr().err == (
        "claude-follow: another animation owns this window — resume it after that one ends\n"
    )
    # The keybinding runs through tmux run-shell, which discards stderr: the
    # refusal has to be SEEN.
    popups = _popup_calls(popen)
    assert len(popups) == 1 and any("Busy" in argument for argument in popups[0])
    assert control.load_pending_animation("@1") == control.PendingApplyEdit([operation], 0.0)


def test_pause_resume_that_finds_the_remainder_gone_after_the_acquire_releases_the_slot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Peeked present, then consumed by a hook's catch-up before our acquire.
    _register_fake_follower("@1", "%2")
    control.save_pending_show_fresh("@1", ("a",), 0.0)
    real_acquire = control.try_acquire_animating

    def acquire_after_a_hook_consumed_it(window_id: str) -> bool:
        control.discard_pending_animation(window_id)
        return real_acquire(window_id)

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
        patch(
            "vim_ai_follower.commands.control.try_acquire_animating",
            side_effect=acquire_after_a_hook_consumed_it,
        ),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    assert capsys.readouterr().out == "claude-follow: nothing to pause\n"
    assert control.is_animating("@1") is False  # released


def test_pause_resume_shows_resuming_popup_before_replay_and_nothing_after_completion() -> None:
    _register_fake_follower("@1", "%2")
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [operation], 0.0)
    order: list[str] = []

    def _tracking_run(command: list[str], **keyword_arguments: object) -> MagicMock:
        if command[:2] == ["tmux", "send-keys"] and "keys" not in order:
            order.append("keys")
        return _mock_tmux_run()(command, **keyword_arguments)

    def _tracking_popen(command: list[str], **keyword_arguments: object) -> MagicMock:
        if command[:2] == ["tmux", "display-popup"]:
            order.append(f"popup:{command[-1]}")
        return MagicMock()

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_tracking_run),
        patch("vim_ai_follower.tmux.subprocess.Popen", side_effect=_tracking_popen),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0

    popups = [entry for entry in order if entry.startswith("popup:")]
    assert len(popups) == 1  # completion needs no second popup — the replay itself is the feedback
    assert "Resuming" in popups[0]
    assert order.index(popups[0]) < order.index("keys")  # popup fired BEFORE the replay


def test_pause_resume_shows_popup_with_interrupted_message_when_resume_is_interrupted() -> None:
    _register_fake_follower("@1", "%2")
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [operation], 0.0)

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0

    popups = _popup_calls(popen)
    assert len(popups) == 2  # "Resuming" up front, then the outcome
    assert any("Resuming" in argument for argument in popups[0])
    assert any("Interrupted" in argument for argument in popups[1])


def test_pause_resume_fails_without_registered_follower(
    capsys: pytest.CaptureFixture[str],
) -> None:
    control.save_pending_show_fresh("@1", ("a",), 0.0, continuation=True)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 1
    assert "no follower registered" in capsys.readouterr().err
    # loading consumed the file; the failure path must put it back intact
    pending = control.load_pending_animation("@1")
    assert pending == control.PendingShowFresh(("a",), 0.0, continuation=True)


def test_pause_resume_without_follower_preserves_pending_apply_edit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("x",))
    control.save_pending_apply_edit("@1", [operation], 0.2)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 1
    assert "no follower registered" in capsys.readouterr().err
    pending = control.load_pending_animation("@1")
    assert pending == control.PendingApplyEdit([operation], 0.2)


def test_interrupt_with_pending_discards_it_and_hands_the_buffer_over() -> None:
    _register_fake_follower("@1", "%2")
    control.save_pending_show_fresh("@1", ("leftover",), 0.15, continuation=True)

    with (
        patch("vim_ai_follower.control.request_pause") as request_pause,
        patch("vim_ai_follower.control.request_interrupt") as request_interrupt,
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0

    assert control.has_pending_animation("@1") is False
    request_pause.assert_not_called()
    request_interrupt.assert_not_called()
    sends = [
        recorded_call.args[0][6]
        for recorded_call in run.call_args_list
        if recorded_call.args[0][:4] == ["tmux", "send-keys", "-t", "%2"]
        and "-l" in recorded_call.args[0]
    ]
    # buffer unlocked for the user, 'paste' given back from its record
    assert call_spelled("hand_over", 0) in [resolve_typed_paths(text) for text in sends]
    popups = _popup_calls(popen)
    assert len(popups) == 1
    assert any("Interrupted" in argument for argument in popups[0])


def test_interrupt_with_pending_but_dead_follower_still_discards_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # no follower registered (e.g. the pane died while paused): there is no
    # buffer to hand over, but the stale pending state must still die
    control.save_pending_show_fresh("@1", ("leftover",), 0.15, continuation=True)
    with (
        patch("vim_ai_follower.control.request_pause") as request_pause,
        patch("vim_ai_follower.control.request_interrupt") as request_interrupt,
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
    assert control.has_pending_animation("@1") is False
    request_pause.assert_not_called()
    request_interrupt.assert_not_called()
    assert _popup_calls(popen) == []  # no follower pane to show it on
    assert "discarded" in capsys.readouterr().out


def test_interrupt_with_pending_resets_current_file_for_resync() -> None:
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="%2 vim\n"),
    ):
        state.FollowerState.set("@1", "tmux", "%2", current_file="/tmp/f.txt")
    control.save_pending_show_fresh("@1", ("leftover",), 0.15, continuation=True)

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0

    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file is None


def test_main_dispatches_pause_and_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        monkeypatch.setenv("TMUX_PANE", "%1")
        assert cli.main(["pause"]) == 0
        assert cli.main(["interrupt"]) == 0


def test_pause_is_a_quiet_noop_when_nothing_is_running(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # After an interrupt (or before anything ran) there is no animation and
    # no pending state — a pause press must not pretend something happened
    _register_fake_follower("@1", "%2")
    with (
        patch("vim_ai_follower.control.request_pause") as request_pause,
        patch("vim_ai_follower.control.request_interrupt") as request_interrupt,
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    request_pause.assert_not_called()
    request_interrupt.assert_not_called()
    assert _popup_calls(popen) == []  # and no lying popup
    assert "nothing to pause" in capsys.readouterr().out


def test_interrupt_is_a_quiet_noop_when_nothing_is_running(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_fake_follower("@1", "%2")
    with (
        patch("vim_ai_follower.control.request_pause") as request_pause,
        patch("vim_ai_follower.control.request_interrupt") as request_interrupt,
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
    request_pause.assert_not_called()
    request_interrupt.assert_not_called()
    assert _popup_calls(popen) == []
    assert "nothing to interrupt" in capsys.readouterr().out


def test_pause_over_a_paused_hook_requests_resume_with_resuming_popup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_fake_follower("@1", "%2")
    control.mark_animating("@1", state="paused")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    assert control.check_signal("@1") == "pause"  # the waiting hook consumes it as resume
    assert "resume requested" in capsys.readouterr().out
    popups = _popup_calls(popen)
    assert len(popups) == 1
    assert any("Resuming" in argument for argument in popups[0])


def test_pause_during_handoff_only_prints_guidance(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_fake_follower("@1", "%2")
    control.mark_animating("@1", state="handoff")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    assert control.check_signal("@1") is None  # no signal: P has no meaning here
    assert _popup_calls(popen) == []
    assert "save (:w!)" in capsys.readouterr().out


def test_interrupt_during_handoff_signals_discard(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_fake_follower("@1", "%2")
    control.mark_animating("@1", state="handoff")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
    assert control.check_signal("@1") == "interrupt"  # the waiting hook des-interrupts
    assert "unsaved changes discarded" in capsys.readouterr().out
    popups = _popup_calls(popen)
    assert len(popups) == 1
    assert any("Discarded" in argument for argument in popups[0])


def test_pause_resume_that_cannot_reach_the_file_keeps_the_remainder(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The navigation failed before anything was typed: the remainder is
    still whole, so it stays recoverable, and the slot is released."""
    _register_fake_follower("@1", "%2")
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [operation], 0.0, file_path="/tmp/f.py")

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
        patch.object(
            TmuxVimFollower, "resume", side_effect=NavigationFailed("did not land (no answer)")
        ),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 1

    assert "could not resume: did not land (no answer)" in capsys.readouterr().err
    assert control.has_pending_animation("@1") is True
    assert control.is_animating("@1") is False


def test_a_resume_that_fails_after_a_pause_keeps_the_drivers_remainder() -> None:
    """Final M1: the replay typed op 0, was paused, and its post-pause goto
    failed. The driver saved the remainder from op 1 before the error came
    out; re-saving the ORIGINAL over it would replay op 0 on the next P."""
    _register_fake_follower("@1", "%2")
    typed = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("typed",))
    left = EditOp(kind="insert", start_line=2, end_line=1, new_lines=("left",))
    control.save_pending_apply_edit("@1", [typed, left], 0.0, file_path="/tmp/f.py", partial="")

    def typed_one_then_failed(*_args: object, **_kwargs: object) -> None:
        control.save_pending_apply_edit("@1", [left], 0.0, file_path="/tmp/f.py", partial="typed\n")
        raise NavigationFailed("did not land (no answer)")

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
        patch.object(TmuxVimFollower, "resume", side_effect=typed_one_then_failed),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 1

    assert control.load_pending_animation("@1") == control.PendingApplyEdit(
        [left], 0.0, file_path="/tmp/f.py", partial="typed\n"
    )


def test_a_resume_cut_short_by_a_stop_does_not_resave_the_remainder() -> None:
    """Review of T6+T7, m3: `claude-follow stop` ran while the replay's goto
    waited (it clears the state and discards the pending), then the goto
    failed. Re-saving the original would leave a remainder for a window
    with no follower."""
    _register_fake_follower("@1", "%2")
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [operation], 0.0, file_path="/tmp/f.py")

    def stopped_then_failed(*_args: object, **_kwargs: object) -> None:
        state.FollowerState.clear("@1")
        control.discard_pending_animation("@1")
        raise NavigationFailed("did not land (no answer)")

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
        patch.object(TmuxVimFollower, "resume", side_effect=stopped_then_failed),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 1

    assert control.has_pending_animation("@1") is False


@pytest.mark.parametrize(
    ("command", "seen_state", "message"),
    [
        (commands.cmd_pause, "running", "claude-follow: nothing to pause\n"),
        (commands.cmd_pause, "paused", "claude-follow: nothing to pause\n"),
        (commands.cmd_interrupt, "running", "claude-follow: nothing to interrupt\n"),
        (commands.cmd_interrupt, "paused", "claude-follow: nothing to interrupt\n"),
        (commands.cmd_interrupt, "handoff", "claude-follow: nothing to interrupt\n"),
    ],
)
def test_a_press_whose_owner_died_before_the_signal_claims_nothing(
    command: object, seen_state: str, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # D1 residual: the marker read `seen_state`, then its owner died before the
    # signal was written. Nothing was written, so nothing may claim success:
    # no "pause requested", no Paused/Interrupted popup.
    _register_fake_follower("@1", "%2")
    with (
        patch("vim_ai_follower.commands.control.animating_state", return_value=seen_state),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert command({"TMUX_PANE": "%1"}) == 0  # type: ignore[operator]
    assert capsys.readouterr().out == message
    assert _popup_calls(popen) == []
    assert control.check_signal("@1") is None


def _nvim_follower() -> None:
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        state.FollowerState.set("@1", "nvim", "/tmp/x.sock")


def test_a_pause_whose_hook_finished_before_its_cue_appeared_takes_the_cue_back() -> None:
    """The hook's exit drain consumed the P and retired "Paused" — before this
    press had put "Paused" up. Nothing would ever take it down, so the press
    re-reads the state after its feedback and retracts it itself."""
    _nvim_follower()
    control.mark_animating("@1")
    surface = MagicMock()

    def hook_drains_meanwhile(text: str) -> None:
        assert text == "Paused"
        control.mark_animating("@1", state="finishing")
        assert control.check_signal("@1") == "pause"

    surface.set_state.side_effect = hook_drains_meanwhile
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.status_surface_for", return_value=surface),
        patch("vim_ai_follower.hooks.status_surface_for", return_value=surface),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    assert surface.method_calls == [call.set_state("Paused"), call.retire_state("Paused")]


def test_a_pause_whose_hook_is_still_running_leaves_its_cue_up() -> None:
    _nvim_follower()
    control.mark_animating("@1")
    surface = MagicMock()
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.status_surface_for", return_value=surface),
        patch("vim_ai_follower.hooks.status_surface_for", return_value=surface),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    assert surface.method_calls == [call.set_state("Paused")]


def test_on_tmux_a_pause_whose_hook_finished_meanwhile_is_answered_after_its_popup() -> None:
    _register_fake_follower("@1", "%2")
    control.mark_animating("@1")

    def popen(command: list[str], **_kwargs: object) -> MagicMock:
        if command[:2] == ["tmux", "display-popup"]:
            control.clear_animating("@1")  # the hook exited while "Paused" opened
        return MagicMock()

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", side_effect=popen) as spawned,
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    commands_run = [entry.args[0] for entry in spawned.call_args_list]
    assert len(commands_run) == 2
    assert "Paused" in commands_run[0][-1]
    assert commands_run[1][:2] == ["sh", "-c"]
    assert commands_run[1][2].startswith(f"sleep {tmux.POPUP_SECONDS + 0.1}; mv -- ")
    assert "Nothing to pause" in commands_run[1][2]


def test_interrupting_an_orphaned_remainder_holds_the_slot_while_it_hands_over() -> None:
    """Slot residual: the crash-orphaned path sent its hand-over keys without
    the window's slot, so a hook arriving meanwhile typed into the same pane."""
    _register_fake_follower("@1", "%2")
    control.save_pending_show_fresh("@1", ("leftover",), 0.15, continuation=True)
    seen: list[str | None] = []

    def hand_over(_self: TmuxVimFollower) -> None:
        seen.append(control.animating_state("@1"))

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
        patch.object(TmuxVimFollower, "hand_over", hand_over),
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
    # "finishing": a P or S pressed meanwhile has nothing to act on.
    assert seen == ["finishing"]
    assert control.is_animating("@1") is False  # released on the way out


def test_interrupting_an_orphaned_remainder_while_a_hook_owns_the_slot_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_fake_follower("@1", "%2")
    control.save_pending_show_fresh("@1", ("leftover",), 0.15, continuation=True)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
        patch("vim_ai_follower.commands.control.try_acquire_animating", return_value=False),
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 1
    assert not [entry for entry in run.call_args_list if entry.args[0][:2] == ["tmux", "send-keys"]]
    assert capsys.readouterr().err == (
        "claude-follow: another animation owns this window — press S again to interrupt it\n"
    )
    popups = _popup_calls(popen)
    assert len(popups) == 1 and any("Busy" in argument for argument in popups[0])
    assert control.has_pending_animation("@1") is True  # the hook's catch-up decides


def test_interrupting_a_remainder_a_hook_consumed_before_the_acquire_releases_the_slot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_fake_follower("@1", "%2")
    control.save_pending_show_fresh("@1", ("a",), 0.0)
    real_acquire = control.try_acquire_animating

    def acquire_after_a_hook_consumed_it(window_id: str, state: str) -> bool:
        control.discard_pending_animation(window_id)
        return real_acquire(window_id, state=state)

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
        patch(
            "vim_ai_follower.commands.control.try_acquire_animating",
            side_effect=acquire_after_a_hook_consumed_it,
        ),
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
    assert capsys.readouterr().out == "claude-follow: nothing to interrupt\n"
    assert not [entry for entry in run.call_args_list if entry.args[0][:2] == ["tmux", "send-keys"]]
    assert control.is_animating("@1") is False


@pytest.mark.parametrize("command", [commands.cmd_pause, commands.cmd_interrupt])
def test_on_nvim_a_busy_refusal_shows_on_the_float_for_a_moment(
    command: object, capsys: pytest.CaptureFixture[str]
) -> None:
    """PF round-2 minor: the Busy refusal was a tmux popup only, and the
    keybinding's run-shell discards stderr — on nvim it was invisible."""
    _nvim_follower()
    control.save_pending_show_fresh("@1", ("leftover",), 0.15, continuation=True)
    events: list[object] = []
    surface = MagicMock()
    surface.set_state.side_effect = lambda text: events.append(("set", text))
    surface.retire_state.side_effect = lambda *texts: events.append(("retire", texts))
    with (
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.status_surface_for", return_value=surface),
        patch("vim_ai_follower.commands.time.sleep", side_effect=events.append),
        patch("vim_ai_follower.commands.control.try_acquire_animating", return_value=False),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert command({"TMUX_PANE": "%1"}) == 1  # type: ignore[operator]
    assert events == [("set", "Busy"), tmux.POPUP_SECONDS, ("retire", ("Busy",))]
    assert _popup_calls(popen) == []
    assert "another animation owns this window" in capsys.readouterr().err
    assert control.has_pending_animation("@1") is True


@pytest.mark.parametrize("command", [commands.cmd_pause, commands.cmd_interrupt])
def test_a_busy_refusal_with_no_live_follower_shows_nothing(
    command: object, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crash left a resumable remainder, the follower is gone (no state),
    and another process holds the slot: the press is refused on stderr, with
    no popup and no float, since there is no follower to show them on."""
    control.save_pending_show_fresh("@1", ("leftover",), 0.15, continuation=True)
    with (
        patch("vim_ai_follower.commands.status_surface_for") as surface_for,
        patch("vim_ai_follower.commands.control.try_acquire_animating", return_value=False),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert command({"TMUX_PANE": "%1"}) == 1  # type: ignore[operator]
    assert _popup_calls(popen) == []
    surface_for.assert_not_called()
    assert "another animation owns this window" in capsys.readouterr().err
    assert control.has_pending_animation("@1") is True


@pytest.mark.parametrize("command", ["pause", "interrupt"])
def test_a_late_correction_never_masks_a_real_paused(command: str) -> None:
    """Sweep-5 T22 (final m2): a drain's "Nothing to pause" correction opens
    1.6 s after the drain, for 1.5 s, and tmux drops a popup opened while
    another is up, so the next hook's real "Paused" was lost behind it. The
    press withdraws every correction scheduled for its window first."""
    _register_fake_follower("@1", "%2")
    scheduled = control.new_correction_token("@1")
    control.mark_animating("@1")
    withdrawn_before_the_popup: list[bool] = []

    def popen(spawned: list[str], **_kwargs: object) -> MagicMock:
        if spawned[:2] == ["tmux", "display-popup"]:
            withdrawn_before_the_popup.append(not scheduled.exists())
        return MagicMock()

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", side_effect=popen),
    ):
        entry = commands.cmd_pause if command == "pause" else commands.cmd_interrupt
        assert entry({"TMUX_PANE": "%1"}) == 0
    assert withdrawn_before_the_popup == [True]


def test_the_keyboard_resume_drains_its_unconsumed_presses() -> None:
    """Sweep-5 T22 (final m3): the keyboard resume holds the slot as
    "running" like a hook, so a P pressed after its replay's last signal
    check is addressed to it, and nothing consumed it: "Paused" stayed up and
    the signal was left for the next hook to drop as stale."""
    _register_fake_follower("@1", "%2")
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [operation], 0.0)
    real_resume = TmuxVimFollower.resume

    def resume_then_a_late_press(follower: TmuxVimFollower, pending: object) -> object:
        result = real_resume(follower, pending)  # type: ignore[arg-type]
        assert control.request_pause("@1")  # after the replay's last check
        return result

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as spawned,
        patch.object(TmuxVimFollower, "resume", resume_then_a_late_press),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    assert not (cache.CACHE_DIRECTORY / "@1.pause").exists()
    corrections = [
        entry.args[0][2] for entry in spawned.call_args_list if entry.args[0][:2] == ["sh", "-c"]
    ]
    assert len(corrections) == 1 and "Nothing to pause" in corrections[0]
