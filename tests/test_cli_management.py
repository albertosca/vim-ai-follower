from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower import cli, config, control, state


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(control, "CONTROL_DIR", tmp_path / "control")


def _mock_tmux_run(
    pane_exists: bool = True, session_id: str = "$1", new_pane_id: str = "%9"
) -> Callable[..., MagicMock]:
    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        if cmd[:3] == ["tmux", "list-panes", "-a"]:
            result.stdout = "%1 zsh\n%2 zsh\n" + (f"{new_pane_id} vim\n" if pane_exists else "")
            result.returncode = 0
        elif cmd[:2] == ["tmux", "display-message"]:
            result.returncode = 0
            result.stdout = f"{session_id}\n"
        elif cmd[:2] == ["tmux", "split-window"]:
            result.returncode = 0
            result.stdout = f"{new_pane_id}\n"
        else:
            result.returncode = 0
            result.stdout = ""
        return result

    return _run


def test_start_without_tmux_env_fails() -> None:
    assert cli.cmd_start({}) == 1


def test_start_creates_and_registers_a_pane() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert cli.cmd_start({"TMUX_PANE": "%1"}) == 0
        result = state.FollowerState.get("$1")
    assert result is not None
    assert result.target == "%9"


def test_start_is_idempotent_when_already_running() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        cli.cmd_start({"TMUX_PANE": "%1"})
        exit_code = cli.cmd_start({"TMUX_PANE": "%1"})
        result = state.FollowerState.get("$1")
    assert exit_code == 0
    assert result is not None
    assert result.target == "%9"


def test_stop_kills_pane_and_clears_state() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        cli.cmd_start({"TMUX_PANE": "%1"})
        exit_code = cli.cmd_stop({"TMUX_PANE": "%1"})
        result = state.FollowerState.get("$1")
    assert exit_code == 0
    assert result is None


def test_stop_without_tmux_env_fails() -> None:
    assert cli.cmd_stop({}) == 1


def test_stop_without_active_follower_is_a_noop() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert cli.cmd_stop({"TMUX_PANE": "%1"}) == 0


def test_status_reports_no_follower(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        cli.cmd_status({"TMUX_PANE": "%1"})
    assert "no follower active" in capsys.readouterr().out


def test_status_reports_active_follower(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        cli.cmd_start({"TMUX_PANE": "%1"})
        cli.cmd_status({"TMUX_PANE": "%1"})
    assert "%9" in capsys.readouterr().out


def test_status_without_tmux_env(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.cmd_status({}) == 0
    assert "not running inside tmux" in capsys.readouterr().out


def test_start_nvim_backend_fails_without_socket(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("pynvim.attach", side_effect=OSError("no such file")),
    ):
        exit_code = cli.cmd_start({"TMUX_PANE": "%1"}, backend="nvim_rpc")
    assert exit_code == 1
    assert "no Neovim RPC socket found" in capsys.readouterr().err


def test_start_resolves_on_failure_and_speed_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"on_failure": "reopen", "speed": "lento"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert cli.cmd_start({"TMUX_PANE": "%1"}) == 0
        result = state.FollowerState.get("$1")
    assert result is not None
    assert result.on_failure == "reopen"
    assert result.speed == "lento"


def test_start_explicit_args_override_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"on_failure": "reopen", "speed": "lento"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert cli.cmd_start({"TMUX_PANE": "%1"}, on_failure="silent", speed="instant") == 0
        result = state.FollowerState.get("$1")
    assert result is not None
    assert result.on_failure == "silent"
    assert result.speed == "instant"


def test_status_reports_on_failure_and_speed(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        cli.cmd_start({"TMUX_PANE": "%1"}, on_failure="reopen", speed="lento")
        cli.cmd_status({"TMUX_PANE": "%1"})
    out = capsys.readouterr().out
    assert "on_failure=reopen" in out
    assert "speed=lento" in out


def test_start_nvim_backend_registers_when_socket_alive() -> None:
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("pynvim.attach", return_value=MagicMock()),
    ):
        assert cli.cmd_start({"TMUX_PANE": "%1"}, backend="nvim_rpc") == 0
        result = state.FollowerState.get("$1")
    assert result is not None
    assert result.backend == "nvim_rpc"


def _bind_calls(run_mock: MagicMock) -> list[list[str]]:
    return [c.args[0] for c in run_mock.call_args_list if c.args[0][:2] == ["tmux", "bind-key"]]


def _unbind_calls(run_mock: MagicMock) -> list[list[str]]:
    return [c.args[0] for c in run_mock.call_args_list if c.args[0][:2] == ["tmux", "unbind-key"]]


def test_start_registers_pause_and_interrupt_keybindings() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_start({"TMUX_PANE": "%1"}) == 0
    binds = _bind_calls(run)
    assert [
        "tmux",
        "bind-key",
        "-T",
        "prefix",
        "P",
        "run-shell",
        'TMUX_PANE=$(tmux display-message -p "#{pane_id}") claude-follow pause',
    ] in binds
    assert [
        "tmux",
        "bind-key",
        "-T",
        "prefix",
        "S",
        "run-shell",
        'TMUX_PANE=$(tmux display-message -p "#{pane_id}") claude-follow interrupt',
    ] in binds


def test_stop_unregisters_keybindings_and_clears_signals() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        cli.cmd_start({"TMUX_PANE": "%1"})
        control.request_pause("$1")
        control.save_pending_apply_edit("$1", [], 0.0)
        assert cli.cmd_stop({"TMUX_PANE": "%1"}) == 0
    unbinds = _unbind_calls(run)
    assert ["tmux", "unbind-key", "-T", "prefix", "P"] in unbinds
    assert ["tmux", "unbind-key", "-T", "prefix", "S"] in unbinds
    assert control.check_signal("$1") is None
    assert control.has_pending_animation("$1") is False
