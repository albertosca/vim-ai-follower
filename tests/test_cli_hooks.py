from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run as _mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import cli, control, snapshot, state


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


def test_hook_post_first_open_retypes_from_scratch_instead_of_pasting(tmp_path: Path) -> None:
    # PostToolUse fires after the file is already written, so `:e` on the
    # first view would already show its final content directly, spoiling
    # the "watch it type" effect. Instead the buffer is renamed in place and
    # wiped, then the whole file is retyped from scratch — `:e` never runs.
    target = tmp_path / "f.txt"
    target.write_text("hello\nworld\n")
    snapshot.save("$1", str(target), "hello\n")
    _register_fake_follower("$1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert not any(text.startswith(":e ") for text in sends)
    assert f":file {target}" in sends
    assert ":%d" in sends
    assert "hello" in sends
    assert "world" in sends


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

    assert _literal_sends(run) == [f":e {target}", ":setlocal readonly nomodifiable"]


def test_hook_post_skips_binary_files_on_subsequent_edit(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"\x00\x01\x02")
    snapshot.save("$1", str(target), "")
    _register_fake_follower("$1", "%2", current_file=str(target))

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == []


def test_hook_post_read_without_offset_does_not_navigate(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\nc\n")
    _register_fake_follower("$1", "%2")

    payload: dict[str, object] = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target)},
    }
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == [f":e {target}", ":setlocal readonly nomodifiable"]


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

    assert _literal_sends(run) == [
        f":e {target}",
        ":setlocal readonly nomodifiable",
        ":2",
    ]


def test_hook_post_read_skips_e_when_file_already_current(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\nc\n")
    _register_fake_follower("$1", "%2", current_file=str(target))

    payload: dict[str, object] = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target), "offset": 2},
    }
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == [":2"]


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


def test_main_start_stop_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        monkeypatch.setenv("TMUX_PANE", "%1")
        assert cli.main(["start"]) == 0
        assert cli.main(["status"]) == 0
        assert "active, backend tmux (%2)" in capsys.readouterr().out
        assert cli.main(["stop"]) == 0
        assert cli.main(["status"]) == 0
        assert "no follower active" in capsys.readouterr().out


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


def test_main_hook_post_reads_stdin_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("x\n")
    payload = f'{{"tool_name": "Bash", "tool_input": {{"file_path": "{target}"}}}}'
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="$1\n"),
    ):
        monkeypatch.setenv("TMUX_PANE", "%1")
        assert cli.main(["hook", "post"]) == 0


def test_hook_post_edit_interrupted_prints_notification_and_leaves_buffer_unlocked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "f.txt"
    target.write_text("hello\nworld\n")
    snapshot.save("$1", str(target), "hello\nworld\n")
    _register_fake_follower("$1", "%2", current_file=str(target))
    target.write_text("hello\nvim ai follower\n")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    # before the single replace op's delete-half's Enter is sent, "interrupt"
    # fires — the ex-command text gets typed but never committed
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.control.check_signal", side_effect=[None, "interrupt"]),
    ):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert not any(text == ":setlocal nomodifiable nopaste" for text in sends)
    out = json.loads(capsys.readouterr().out)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert str(target) in context
    assert "hello\nworld" in context  # nothing completed yet — still the pre-edit text


def test_hook_post_first_open_interrupted_prints_notification_with_partial_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\nc\n")
    _register_fake_follower("$1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    # line "a" fully types (3 checks: i, a, Escape); "interrupt" fires on the
    # very first check of line "b"
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.control.check_signal", side_effect=[None, None, None, "interrupt"]),
    ):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert not any(text == ":setlocal readonly nomodifiable nopaste" for text in sends)
    out = json.loads(capsys.readouterr().out)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert context.count("\n\na\n\n") == 1  # only the first line had been typed


def test_hook_post_fast_forwards_pending_before_animating_same_file(tmp_path: Path) -> None:
    from vim_ai_follower.diff import EditOp

    target = tmp_path / "f.txt"
    target.write_text("new content\n")
    _register_fake_follower("$1", "%2", current_file=str(target))
    snapshot.save("$1", str(target), "old content\n")
    control.save_pending_apply_edit(
        "$1", [EditOp(kind="insert", start_line=1, end_line=0, new_lines=("leftover",))], 0.15
    )

    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert "leftover" in sends  # the paused remainder replayed...
    assert sends.index("leftover") < sends.index("new content")  # ...BEFORE the new edit
    assert control.has_pending_animation("$1") is False


def test_hook_post_discards_pending_when_switching_files(tmp_path: Path) -> None:
    old = tmp_path / "old.txt"
    new = tmp_path / "new.txt"
    new.write_text("fresh\n")
    _register_fake_follower("$1", "%2", current_file=str(old))
    control.save_pending_show_fresh("$1", ("leftover",), 0.15, continuation=True)

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(new)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    # show_fresh wipes the buffer anyway — replaying into it would be garbage
    assert "leftover" not in _literal_sends(run)
    assert control.has_pending_animation("$1") is False


def test_hook_post_discards_pending_on_binary_files_too(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\x00\x01\x02")
    _register_fake_follower("$1", "%2", current_file=str(target))
    control.save_pending_show_fresh("$1", ("leftover",), 0.15, continuation=True)

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert "leftover" not in _literal_sends(run)
    assert control.has_pending_animation("$1") is False


def test_hook_post_interrupt_resets_current_file_for_resync(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("line one\nline two\n")
    _register_fake_follower("$1", "%2", current_file=str(target))
    snapshot.save("$1", str(target), "")

    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
    ):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    refreshed = state.FollowerState.read("$1")
    assert refreshed is not None
    assert refreshed.current_file is None  # next edit resyncs via a full retype


def test_hook_post_fresh_interrupt_also_resets_current_file(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\n")
    _register_fake_follower("$1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
    ):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    refreshed = state.FollowerState.read("$1")
    assert refreshed is not None
    assert refreshed.current_file is None


def test_configure_logging_is_idempotent() -> None:
    cli._configure_logging()
    handler = cli.logger.handlers[0]
    cli._configure_logging()  # a second hook in the same process must not stack handlers
    assert cli.logger.handlers == [handler]
