from __future__ import annotations

import functools
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from helpers import make_mock_tmux_run

from vim_ai_follower import cli, state

_mock_tmux_run = functools.partial(make_mock_tmux_run, pane_id="%9", other_panes=("%1",))


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


def test_dead_adopted_follower_reopens_fresh_not_adopted(tmp_path: Path) -> None:
    """Recovery never re-adopts: when an adopted follower dies and reopens,
    the fresh pane must have adopted=False and shown_any=False. This ensures
    that even if someone threads raw.adopted through the recovery path in
    _get_active_follower, the test catches it."""
    target = tmp_path / "f.txt"
    target.write_text("content\n")
    # Initial state: adopted with shown_any, dead target pane %2
    state.FollowerState.set(
        "$1",
        "tmux",
        "%2",
        origin="%1",
        on_failure="reopen",
        adopted=True,
        shown_any=True,
    )

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    # Mock tmux: %2 does not exist (pane_exists=False), split-window returns %9
    mock_run = _mock_tmux_run(pane_exists=False)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=mock_run) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _split_calls(run) == [
        ["tmux", "split-window", "-h", "-t", "%1", "-P", "-F", "#{pane_id}", "vim"]
    ]
    recovered = state.FollowerState.read("$1")
    assert recovered is not None
    assert recovered.target == "%9"
    assert recovered.adopted is False
    assert recovered.shown_any is False
