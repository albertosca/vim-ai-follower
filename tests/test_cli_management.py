from __future__ import annotations

import functools
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import cli, config, control, state

_mock_tmux_run = functools.partial(make_mock_tmux_run, pane_id="%9", other_panes=("%1", "%2"))


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


def test_start_registers_keybindings_with_absolute_path_and_silenced_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "venv-bin"
    fake_bin.mkdir()
    (fake_bin / "claude-follow").touch()
    monkeypatch.setattr(sys, "executable", str(fake_bin / "python"))
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_start({"TMUX_PANE": "%1"}) == 0
    binds = _bind_calls(run)
    assert [cmd[4] for cmd in binds] == ["P", "S", "+", "_"]
    for cmd, subcommand in zip(
        binds, ("pause", "interrupt", "speed-up", "speed-down"), strict=True
    ):
        # run-shell without -b blocks ALL tmux input until the command
        # exits — a resume replay lasts tens of seconds, freezing the user
        assert cmd[6] == "-b"
        shell_command = cmd[-1]
        # bare "claude-follow" resolves to nothing under the tmux server's
        # PATH (exit 127) — the binding must embed the venv's absolute path
        assert str(fake_bin / "claude-follow") in shell_command
        # any stdout inside run-shell throws the pane into a view-mode overlay
        assert f" {subcommand} >/dev/null 2>&1" in shell_command
        # tmux pre-expands #{pane_id} in the run-shell string at keypress
        # time; nesting $(tmux display-message -p "%N") double-expands and
        # display-message EATS the % (formats again), yielding an invalid
        # pane target — TMUX_PANE must take the pre-expanded id directly
        assert shell_command.startswith("TMUX_PANE=#{pane_id} ")
        assert "display-message" not in shell_command


def test_claude_follow_executable_falls_back_to_which_then_bare_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nowhere" / "python"))
    with patch("vim_ai_follower.cli.shutil.which", return_value="/opt/bin/claude-follow"):
        assert cli._claude_follow_executable() == "/opt/bin/claude-follow"
    with patch("vim_ai_follower.cli.shutil.which", return_value=None):
        assert cli._claude_follow_executable() == "claude-follow"


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


def test_stop_restores_a_pre_existing_binding() -> None:
    previous = "bind-key -T prefix P paste-buffer"

    def _run_with_existing_binding(cmd: list[str], **kwargs: object) -> MagicMock:
        # Full-table listing (no per-key filter arg): the caller extracts
        # P's line itself and finds nothing for S.
        if cmd[:4] == ["tmux", "list-keys", "-T", "prefix"]:
            return MagicMock(returncode=0, stdout=previous + "\n")
        return _mock_tmux_run()(cmd, **kwargs)

    with patch(
        "vim_ai_follower.tmux.subprocess.run", side_effect=_run_with_existing_binding
    ) as run:
        assert cli.cmd_start({"TMUX_PANE": "%1"}) == 0
        assert cli.cmd_stop({"TMUX_PANE": "%1"}) == 0

    rebinds = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "bind-key"]]
    assert ["tmux", "bind-key", "-T", "prefix", "P", "paste-buffer"] in rebinds
    unbinds = _unbind_calls(run)
    assert ["tmux", "unbind-key", "-T", "prefix", "S"] in unbinds  # S had no previous binding
    assert not cli._saved_bindings_path().exists()


def test_stop_unbinds_instead_of_restoring_a_stale_claude_follow_binding() -> None:
    stale = 'bind-key -T prefix P run-shell "/old/venv/claude-follow pause >/dev/null 2>&1"'

    def _run_with_stale(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:4] == ["tmux", "list-keys", "-T", "prefix"]:
            return MagicMock(returncode=0, stdout=stale + "\n")
        return _mock_tmux_run()(cmd, **kwargs)

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_run_with_stale) as run:
        assert cli.cmd_start({"TMUX_PANE": "%1"}) == 0
        assert cli.cmd_stop({"TMUX_PANE": "%1"}) == 0

    # "restoring" our own leftover binding from a crashed run would resurrect
    # a possibly-broken path — unbind is the correct cleanup
    assert ["tmux", "unbind-key", "-T", "prefix", "P"] in _unbind_calls(run)


def test_restart_after_crash_does_not_overwrite_the_saved_original_binding() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert cli.cmd_start({"TMUX_PANE": "%1"}) == 0
    saved_path = cli._saved_bindings_path()
    first = saved_path.read_text()

    # simulate a crash: follower state lost, tmux bindings (ours) still live
    state.FollowerState.clear("$1")

    def _run_with_our_binding(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:3] == ["tmux", "list-keys", "-T"]:
            return MagicMock(
                returncode=0,
                stdout='bind-key -T prefix P run-shell "/x/claude-follow pause"\n',
            )
        return _mock_tmux_run()(cmd, **kwargs)

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_run_with_our_binding):
        assert cli.cmd_start({"TMUX_PANE": "%1"}) == 0

    # the re-registration must NOT record our own still-bound key as the
    # user's "previous" binding — the original record wins
    assert saved_path.read_text() == first


def test_stop_with_corrupt_saved_bindings_falls_back_to_unbind() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert cli.cmd_start({"TMUX_PANE": "%1"}) == 0
    cli._saved_bindings_path().write_text("{broken")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_stop({"TMUX_PANE": "%1"}) == 0
    unbinds = _unbind_calls(run)
    assert ["tmux", "unbind-key", "-T", "prefix", "P"] in unbinds
    assert ["tmux", "unbind-key", "-T", "prefix", "S"] in unbinds


def test_speed_up_steps_state_and_reports(capsys: pytest.CaptureFixture[str]) -> None:
    _register_fake_follower("$1", "%2")
    state.FollowerState.update("$1", speed="rapido")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert cli.cmd_speed({"TMUX_PANE": "%1"}, "up") == 0
    result = state.FollowerState.read("$1")
    assert result is not None
    assert result.speed == "muito_rapido"
    assert "speed muito_rapido" in capsys.readouterr().out
    popups = [c.args[0] for c in popen.call_args_list if c.args[0][:2] == ["tmux", "display-popup"]]
    assert any("Speed: muito_rapido" in arg for arg in popups[0])


def test_speed_wraps_round_robin(capsys: pytest.CaptureFixture[str]) -> None:
    _register_fake_follower("$1", "%2")
    state.FollowerState.update("$1", speed="instant")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
    ):
        assert cli.cmd_speed({"TMUX_PANE": "%1"}, "up") == 0
    result = state.FollowerState.read("$1")
    assert result is not None
    assert result.speed == "lento"
    assert "speed lento" in capsys.readouterr().out


def test_speed_without_follower_is_honest_noop(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert cli.cmd_speed({"TMUX_PANE": "%1"}, "down") == 0
    out = capsys.readouterr().out
    assert "no follower active" in out
    assert popen.call_args_list == []
