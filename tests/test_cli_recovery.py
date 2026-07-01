from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower import cli, snapshot, state


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(snapshot, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "hook.log")


def _mock_tmux_run(
    session_id: str = "$1", origin_pane: str = "%1", new_pane_id: str = "%9"
) -> Callable[..., MagicMock]:
    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        if cmd[:3] == ["tmux", "list-panes", "-a"]:
            result.returncode = 0
            result.stdout = f"{origin_pane} zsh\n{new_pane_id} vim\n"
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


def _split_calls(run_mock: MagicMock) -> list[list[str]]:
    return [c.args[0] for c in run_mock.call_args_list if c.args[0][:2] == ["tmux", "split-window"]]


def test_hook_post_reopens_dead_follower_when_configured(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("content\n")
    state.FollowerState.set("$1", "tmux", "%2", origin="%1", on_failure="reopen")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _split_calls(run) == [
        ["tmux", "split-window", "-h", "-t", "%1", "-P", "-F", "#{pane_id}", "vim"]
    ]
    recovered = state.FollowerState.read("$1")
    assert recovered is not None
    assert recovered.target == "%9"
    assert recovered.on_failure == "reopen"


def test_hook_post_stays_silent_when_dead_and_on_failure_is_silent(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("content\n")
    state.FollowerState.set("$1", "tmux", "%2", origin="%1", on_failure="silent")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _split_calls(run) == []
    unchanged = state.FollowerState.read("$1")
    assert unchanged is not None
    assert unchanged.target == "%2"


def test_hook_post_does_not_reopen_nvim_rpc_backend(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("content\n")
    state.FollowerState.set("$1", "nvim_rpc", "/tmp/x.sock", origin="%1", on_failure="reopen")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.backends.nvim_rpc.pynvim.attach", side_effect=OSError("gone")),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
    ):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _split_calls(run) == []


def test_hook_post_stays_silent_when_reopen_has_no_origin(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("content\n")
    state.FollowerState.set("$1", "tmux", "%2", origin="", on_failure="reopen")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _split_calls(run) == []


def test_hook_post_logs_and_stays_silent_when_reopen_fails(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("content\n")
    state.FollowerState.set("$1", "tmux", "%2", origin="%1", on_failure="reopen")
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}

    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        if cmd[:3] == ["tmux", "list-panes", "-a"]:
            result.returncode = 0
            result.stdout = ""
        elif cmd[:2] == ["tmux", "display-message"]:
            result.returncode = 0
            result.stdout = "$1\n"
        elif cmd[:2] == ["tmux", "split-window"]:
            raise subprocess.CalledProcessError(1, cmd)
        else:
            result.returncode = 0
            result.stdout = ""
        return result

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_run):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0
