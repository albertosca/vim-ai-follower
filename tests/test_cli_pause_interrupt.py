from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run as _mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import cli, commands, control, session, state
from vim_ai_follower.diff import EditOp


def _popup_calls(popen_mock: MagicMock) -> list[list[str]]:
    return [
        c.args[0] for c in popen_mock.call_args_list if c.args[0][:2] == ["tmux", "display-popup"]
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
    assert any("Paused" in arg for arg in popups[0])


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
    assert any("Interrupted" in arg for arg in popups[0])


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
        patch("pynvim.attach", return_value=MagicMock()),
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
        patch("pynvim.attach", return_value=MagicMock()),
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
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [op], 0.0)  # crash fallback, no live marker

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
        # get() -> is_alive() must see the adopted nvim as alive.
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=MagicMock()),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0

    assert control.has_pending_animation("@1") is False
    assert "nothing to resume" in capsys.readouterr().out


def test_pause_resumes_pending_apply_edit(capsys: pytest.CaptureFixture[str]) -> None:
    _register_fake_follower("@1", "%2")
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [op], 0.0)

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0

    sends = [
        c.args[0][6]
        for c in run.call_args_list
        if c.args[0][:4] == ["tmux", "send-keys", "-t", "%2"] and "-l" in c.args[0]
    ]
    assert "resumed" in sends
    assert control.has_pending_animation("@1") is False
    assert "resumed (completed)" in capsys.readouterr().out


def test_pause_resume_shows_resuming_popup_before_replay_and_nothing_after_completion() -> None:
    _register_fake_follower("@1", "%2")
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [op], 0.0)
    order: list[str] = []

    def _tracking_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["tmux", "send-keys"] and "keys" not in order:
            order.append("keys")
        return _mock_tmux_run()(cmd, **kwargs)

    def _tracking_popen(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["tmux", "display-popup"]:
            order.append(f"popup:{cmd[-1]}")
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
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("@1", [op], 0.0)

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0

    popups = _popup_calls(popen)
    assert len(popups) == 2  # "Resuming" up front, then the outcome
    assert any("Resuming" in arg for arg in popups[0])
    assert any("Interrupted" in arg for arg in popups[1])


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
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("x",))
    control.save_pending_apply_edit("@1", [op], 0.2)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 1
    assert "no follower registered" in capsys.readouterr().err
    pending = control.load_pending_animation("@1")
    assert pending == control.PendingApplyEdit([op], 0.2)


def test_interrupt_with_pending_discards_it_and_hands_the_buffer_over() -> None:
    _register_fake_follower("@1", "%2")
    control.save_pending_show_fresh("@1", ("leftover",), 0.15, continuation=True)

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0

    assert control.has_pending_animation("@1") is False
    assert control.check_signal("@1") is None  # no orphan signal file left behind
    sends = [
        c.args[0][6]
        for c in run.call_args_list
        if c.args[0][:4] == ["tmux", "send-keys", "-t", "%2"] and "-l" in c.args[0]
    ]
    assert ":setlocal modifiable nopaste" in sends  # buffer unlocked for the user
    popups = _popup_calls(popen)
    assert len(popups) == 1
    assert any("Interrupted" in arg for arg in popups[0])


def test_interrupt_with_pending_but_dead_follower_still_discards_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # no follower registered (e.g. the pane died while paused): there is no
    # buffer to hand over, but the stale pending state must still die
    control.save_pending_show_fresh("@1", ("leftover",), 0.15, continuation=True)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
    assert control.has_pending_animation("@1") is False
    assert control.check_signal("@1") is None
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
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_pause({"TMUX_PANE": "%1"}) == 0
    assert control.check_signal("@1") is None  # no orphan signal file
    assert _popup_calls(popen) == []  # and no lying popup
    assert "nothing to pause" in capsys.readouterr().out


def test_interrupt_is_a_quiet_noop_when_nothing_is_running(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_fake_follower("@1", "%2")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
    assert control.check_signal("@1") is None
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
    assert any("Resuming" in arg for arg in popups[0])


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
    assert any("Discarded" in arg for arg in popups[0])
