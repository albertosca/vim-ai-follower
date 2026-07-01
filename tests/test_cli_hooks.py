from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower import cli, snapshot, state


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(snapshot, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "hook.log")
    yield
    cli.logger.handlers.clear()


def _register_fake_follower(session_id: str, pane_id: str, current_file: str | None = None) -> None:
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout=f"{pane_id}\n"),
    ):
        state.FollowerState.set(session_id, "tmux", pane_id, current_file=current_file)


def _mock_tmux_run(session_id: str = "$1", pane_id: str = "%2") -> Callable[..., MagicMock]:
    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        if cmd[:3] == ["tmux", "list-panes", "-a"]:
            result.returncode = 0
            result.stdout = f"{pane_id}\n"
        elif cmd[:2] == ["tmux", "display-message"]:
            result.returncode = 0
            result.stdout = f"{session_id}\n"
        else:
            result.returncode = 0
            result.stdout = ""
        return result

    return _run


def _literal_sends(run_mock: MagicMock) -> list[str]:
    sends = []
    for call in run_mock.call_args_list:
        cmd = call.args[0]
        if cmd[:4] == ["tmux", "send-keys", "-t", "%2"] and "-l" in cmd:
            sends.append(cmd[6])
    return sends


def test_hook_pre_ignores_non_edit_tools() -> None:
    payload: dict[str, object] = {"tool_name": "Bash", "tool_input": {}}
    assert cli.cmd_hook_pre({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_pre_saves_snapshot_of_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("original\n")
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="$1\n"),
    ):
        assert cli.cmd_hook_pre({"TMUX_PANE": "%1"}, payload) == 0
    assert snapshot.load("$1", str(target)) == "original\n"


def test_hook_pre_saves_empty_snapshot_for_new_file(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="$1\n"),
    ):
        assert cli.cmd_hook_pre({"TMUX_PANE": "%1"}, payload) == 0
    assert snapshot.load("$1", str(target)) == ""


def test_hook_pre_noop_without_file_path() -> None:
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {}}
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="$1\n"),
    ):
        assert cli.cmd_hook_pre({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_pre_noop_without_tmux_env() -> None:
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/f.txt"}}
    assert cli.cmd_hook_pre({}, payload) == 0


def test_hook_post_noop_when_no_follower_registered(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("content\n")
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="$1\n"),
    ):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_post_edit_noop_without_tmux_env() -> None:
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/f.txt"}}
    assert cli.cmd_hook_post({}, payload) == 0


def test_hook_post_edit_noop_without_file_path() -> None:
    _register_fake_follower("$1", "%2")
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_post_edit_logs_and_noops_when_file_unreadable(tmp_path: Path) -> None:
    target = tmp_path / "gone.txt"
    _register_fake_follower("$1", "%2")
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_post_read_noop_without_tmux_env() -> None:
    payload: dict[str, object] = {"tool_name": "Read", "tool_input": {"file_path": "/tmp/f.txt"}}
    assert cli.cmd_hook_post({}, payload) == 0


def test_hook_post_read_noop_when_no_follower_registered(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\n")
    payload: dict[str, object] = {"tool_name": "Read", "tool_input": {"file_path": str(target)}}
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="$1\n"),
    ):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_post_read_noop_without_file_path() -> None:
    _register_fake_follower("$1", "%2")
    payload: dict[str, object] = {"tool_name": "Read", "tool_input": {}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_post_first_open_shows_file_without_animating(tmp_path: Path) -> None:
    # PostToolUse fires after the file is already written, so the first
    # `:e` for a file already loads its final content — animating a
    # before->after diff on top of that would duplicate/garble it.
    target = tmp_path / "f.txt"
    target.write_text("hello\nworld\n")
    snapshot.save("$1", str(target), "hello\n")
    _register_fake_follower("$1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == [f":e {target}"]


def test_hook_post_animates_a_text_edit_on_subsequent_change(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("hello\nworld\n")
    snapshot.save("$1", str(target), "hello\nworld\n")
    _register_fake_follower("$1", "%2", current_file=str(target))

    target.write_text("hello\nvim ai follower\n")
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert "vim ai follower" in _literal_sends(run)


def test_hook_post_skips_binary_files_on_first_open(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"\x00\x01\x02")
    snapshot.save("$1", str(target), "")
    _register_fake_follower("$1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == [f":e {target}"]


def test_hook_post_skips_binary_files_on_subsequent_edit(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"\x00\x01\x02")
    snapshot.save("$1", str(target), "")
    _register_fake_follower("$1", "%2", current_file=str(target))

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == []


def test_hook_post_read_navigates_to_file_and_offset(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\nc\n")
    _register_fake_follower("$1", "%2")

    payload: dict[str, object] = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target), "offset": 2},
    }
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == [f":e {target}", ":2"]


def test_hook_post_skips_e_when_file_already_current(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\n")
    _register_fake_follower("$1", "%2", current_file=str(target))
    snapshot.save("$1", str(target), "a\nb\n")

    target.write_text("a\nX\n")
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert not any(text.startswith(":e ") for text in sends)
    assert "X" in sends


def test_hook_post_ignores_unrelated_tools() -> None:
    payload: dict[str, object] = {"tool_name": "Bash", "tool_input": {}}
    assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


def test_main_start_stop_status(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="%9\n"),
    ):
        monkeypatch.setenv("TMUX_PANE", "%1")
        assert cli.main(["start"]) == 0
        assert cli.main(["status"]) == 0
        assert cli.main(["stop"]) == 0


def test_main_hook_pre_reads_stdin_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("x\n")
    payload = f'{{"tool_name": "Edit", "tool_input": {{"file_path": "{target}"}}}}'
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="$1\n"),
    ):
        monkeypatch.setenv("TMUX_PANE", "%1")
        assert cli.main(["hook", "pre"]) == 0
