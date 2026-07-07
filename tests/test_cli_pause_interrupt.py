from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower import cli, control, snapshot, state
from vim_ai_follower.diff import EditOp


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(snapshot, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(control, "CONTROL_DIR", tmp_path / "control")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "hook.log")


def _mock_tmux_run(session_id: str = "$1", pane_id: str = "%2") -> Callable[..., MagicMock]:
    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        if cmd[:3] == ["tmux", "list-panes", "-a"]:
            result.returncode = 0
            result.stdout = f"{pane_id} vim\n"
        elif cmd[:2] == ["tmux", "display-message"]:
            result.returncode = 0
            result.stdout = f"{session_id}\n"
        else:
            result.returncode = 0
            result.stdout = ""
        return result

    return _run


def _register_fake_follower(session_id: str, pane_id: str) -> None:
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout=f"{pane_id} vim\n"),
    ):
        state.FollowerState.set(session_id, "tmux", pane_id)


def _popup_calls(run_mock: MagicMock) -> list[list[str]]:
    return [
        c.args[0] for c in run_mock.call_args_list if c.args[0][:2] == ["tmux", "display-popup"]
    ]


def test_pause_without_tmux_env_fails() -> None:
    assert cli.cmd_pause({}) == 1


def test_interrupt_without_tmux_env_fails() -> None:
    assert cli.cmd_interrupt({}) == 1


def test_pause_requests_pause_when_nothing_pending(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert cli.cmd_pause({"TMUX_PANE": "%1"}) == 0
    assert control.check_signal("$1") == "pause"
    assert "pause requested" in capsys.readouterr().out


def test_interrupt_requests_interrupt(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert cli.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
    assert control.check_signal("$1") == "interrupt"
    assert "interrupt requested" in capsys.readouterr().out


def test_pause_shows_a_popup_on_the_follower_pane_when_requesting_pause() -> None:
    _register_fake_follower("$1", "%2")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_pause({"TMUX_PANE": "%1"}) == 0
    popups = _popup_calls(run)
    assert len(popups) == 1
    assert popups[0][:3] == ["tmux", "display-popup", "-t"]
    assert popups[0][3] == "%2"
    assert "-E" in popups[0]
    assert any("Pausado" in arg for arg in popups[0])


def test_interrupt_shows_a_popup_on_the_follower_pane() -> None:
    _register_fake_follower("$1", "%2")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
    popups = _popup_calls(run)
    assert len(popups) == 1
    assert popups[0][3] == "%2"
    assert any("Interrompido" in arg for arg in popups[0])


def test_pause_skips_popup_when_no_follower_is_registered() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_pause({"TMUX_PANE": "%1"}) == 0
    assert _popup_calls(run) == []


def test_interrupt_skips_popup_when_no_follower_is_registered() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_interrupt({"TMUX_PANE": "%1"}) == 0
    assert _popup_calls(run) == []


def test_pause_skips_popup_for_nvim_rpc_backend() -> None:
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="$1\n"),
    ):
        state.FollowerState.set("$1", "nvim_rpc", "/tmp/x.sock")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_pause({"TMUX_PANE": "%1"}) == 0
    assert _popup_calls(run) == []


def test_pause_resumes_pending_apply_edit(capsys: pytest.CaptureFixture[str]) -> None:
    _register_fake_follower("$1", "%2")
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("$1", [op], 0.0)

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_pause({"TMUX_PANE": "%1"}) == 0

    sends = [
        c.args[0][6]
        for c in run.call_args_list
        if c.args[0][:4] == ["tmux", "send-keys", "-t", "%2"] and "-l" in c.args[0]
    ]
    assert "resumed" in sends
    assert control.has_pending_animation("$1") is False
    assert "resumed (completed)" in capsys.readouterr().out


def test_pause_resume_shows_popup_with_resumed_message() -> None:
    _register_fake_follower("$1", "%2")
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("$1", [op], 0.0)

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_pause({"TMUX_PANE": "%1"}) == 0

    popups = _popup_calls(run)
    assert len(popups) == 1
    assert popups[0][3] == "%2"
    assert any("Retomado" in arg for arg in popups[0])


def test_pause_resume_shows_popup_with_interrupted_message_when_resume_is_interrupted() -> None:
    _register_fake_follower("$1", "%2")
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("resumed",))
    control.save_pending_apply_edit("$1", [op], 0.0)

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
    ):
        assert cli.cmd_pause({"TMUX_PANE": "%1"}) == 0

    popups = _popup_calls(run)
    assert len(popups) == 1
    assert any("Interrompido" in arg for arg in popups[0])


def test_pause_resume_fails_without_registered_follower(
    capsys: pytest.CaptureFixture[str],
) -> None:
    control.save_pending_show_fresh("$1", ("a",), 0.0)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert cli.cmd_pause({"TMUX_PANE": "%1"}) == 1
    assert "no follower registered" in capsys.readouterr().err
    assert control.has_pending_animation("$1") is True  # left untouched, not consumed


def test_main_dispatches_pause_and_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        monkeypatch.setenv("TMUX_PANE", "%1")
        assert cli.main(["pause"]) == 0
        assert cli.main(["interrupt"]) == 0
