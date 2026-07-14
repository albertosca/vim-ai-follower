from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run as _mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import cli, config, control, snapshot, state


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
    _register_fake_follower(
        "$1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )

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

    assert _literal_sends(run) == [f":tab drop {target}", ":setlocal readonly nomodifiable"]


def test_hook_post_skips_binary_files_on_subsequent_edit(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"\x00\x01\x02")
    snapshot.save("$1", str(target), "")
    _register_fake_follower(
        "$1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )

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

    assert _literal_sends(run) == [f":tab drop {target}", ":setlocal readonly nomodifiable"]


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
        f":tab drop {target}",
        ":setlocal readonly nomodifiable",
        ":2",
    ]


def test_hook_post_read_navigates_even_when_file_already_current(tmp_path: Path) -> None:
    # The preamble always runs now — it's cheap and self-healing, immune to
    # the user having closed or reordered tabs since the last time this file
    # was current, so there's no early-return to skip it.
    target = tmp_path / "f.txt"
    target.write_text("a\nb\nc\n")
    _register_fake_follower("$1", "%2", current_file=str(target))

    payload: dict[str, object] = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target), "offset": 2},
    }
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == [
        f":tab drop {target}",
        ":setlocal readonly nomodifiable",
        ":2",
    ]


def test_hook_post_skips_e_when_file_already_current(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\n")
    _register_fake_follower(
        "$1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )
    snapshot.save("$1", str(target), "a\nb\n")

    target.write_text("a\nX\n")
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert not any(text.startswith(":e ") for text in sends)
    assert "X" in sends


def test_edit_of_untracked_file_is_fresh_and_updates_open_files(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    b.write_text("print('b')\n")
    # b was never shown before (open_files only tracks a) — freshness is
    # driven by open_files membership, not current_file.
    _register_fake_follower("$1", "%2", current_file=str(a), open_files=(str(a),), shown_any=True)

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(b)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert ":tabnew" in sends  # show_fresh's in_new_tab, since shown_any was already True
    assert f":file {b}" in sends  # renamed in place — the show_fresh path, not apply_edit

    refreshed = state.FollowerState.read("$1")
    assert refreshed is not None
    assert refreshed.open_files == (str(a), str(b))
    assert refreshed.shown_any is True


def test_edit_of_tracked_file_uses_apply_edit_even_when_not_current(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("print('a')\n")
    snapshot.save("$1", str(a), "print('a')\n")
    a.write_text("print('a changed')\n")
    # b is the current tab, but the edit targets a — already tracked, so it's
    # not fresh even though it's not the current file.
    _register_fake_follower(
        "$1", "%2", current_file=str(b), open_files=(str(a), str(b)), shown_any=True
    )

    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(a)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    # apply_edit's goto_file preamble navigates to a's tab — the backend's
    # job, not cli.py's — then the diff is applied in place; show_fresh's
    # rename-in-place (":file <path>") never runs.
    assert f":tab drop {a}" in sends
    assert not any(text.startswith(":file ") for text in sends)
    assert "print('a changed')" in sends


def test_eviction_closes_oldest_tab_before_animating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"max_tabs": 2}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    c = tmp_path / "c.py"
    c.write_text("print('c')\n")
    _register_fake_follower(
        "$1", "%2", current_file=str(b), open_files=(str(a), str(b)), shown_any=True
    )

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(c)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    # a's tab is closed (close_tab: drop, bwipeout, tabclose) BEFORE c's
    # content starts animating (show_fresh's rename-in-place for c).
    close_index = sends.index(":silent! tabclose")
    rename_index = sends.index(f":file {c}")
    assert close_index < rename_index

    refreshed = state.FollowerState.read("$1")
    assert refreshed is not None
    assert refreshed.open_files == (str(b), str(c))


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


def _interrupt_then_user_saves(
    target: Path, at_check: int, saved_content: str = "user version\n"
) -> Callable[..., str | None]:
    """check_signal stand-in: fire the interrupt at the Nth check, then —
    from inside the hand-off wait loop — simulate the user's :w! by
    rewriting the file, which releases the hook with the notification."""
    calls = {"n": 0}

    def _check(session_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == at_check:
            return "interrupt"
        if calls["n"] > at_check:
            target.write_text(saved_content)
        return None

    return _check


def test_hook_post_edit_interrupted_prints_notification_and_leaves_buffer_unlocked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "f.txt"
    target.write_text("hello\nworld\n")
    snapshot.save("$1", str(target), "hello\nworld\n")
    _register_fake_follower(
        "$1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )
    target.write_text("hello\nvim ai follower\n")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    # before the single replace op's delete-half's Enter is sent, "interrupt"
    # fires — the ex-command text gets typed but never committed
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=_interrupt_then_user_saves(target, at_check=2),
        ),
        patch("vim_ai_follower.cli.time.sleep"),
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
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=_interrupt_then_user_saves(target, at_check=4),
        ),
        patch("vim_ai_follower.cli.time.sleep"),
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
    _register_fake_follower(
        "$1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )
    snapshot.save("$1", str(target), "old content\n")
    control.save_pending_apply_edit(
        "$1",
        [EditOp(kind="insert", start_line=1, end_line=0, new_lines=("leftover",))],
        0.15,
        file_path=str(target),
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
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=_interrupt_then_user_saves(target, at_check=1),
        ),
        patch("vim_ai_follower.cli.time.sleep"),
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
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=_interrupt_then_user_saves(target, at_check=1),
        ),
        patch("vim_ai_follower.cli.time.sleep"),
    ):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    refreshed = state.FollowerState.read("$1")
    assert refreshed is not None
    assert refreshed.current_file is None


def test_hooks_are_noops_while_disabled(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("original\n")
    _register_fake_follower(
        "$1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )
    state.FollowerState.update("$1", enabled=False)

    pre_payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.cli.save_snapshot") as save_mock,
    ):
        assert cli.cmd_hook_pre({"TMUX_PANE": "%1"}, pre_payload) == 0
    save_mock.assert_not_called()

    target.write_text("changed\n")
    post_payload: dict[str, object] = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
    }
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, post_payload) == 0
    assert _literal_sends(run) == []

    read_payload: dict[str, object] = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target)},
    }
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, read_payload) == 0
    assert _literal_sends(run) == []


def test_configure_logging_is_idempotent() -> None:
    cli._configure_logging()
    handler = cli.logger.handlers[0]
    cli._configure_logging()  # a second hook in the same process must not stack handlers
    assert cli.logger.handlers == [handler]


def test_hook_post_des_interrupt_reverts_and_resumes_following(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\n")
    _register_fake_follower("$1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    # interrupt fires at the first check; a second S during the hand-off
    # wait means "discard my unsaved typing and put the show back on"
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.control.check_signal", side_effect=["interrupt", "interrupt"]),
        patch("vim_ai_follower.cli.time.sleep"),
    ):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert ":e!" in sends  # unsaved user edits discarded, real file loaded
    assert ":setlocal readonly nomodifiable" in sends  # relocked
    # the defensive `:tab drop` preamble lands on the file's tab (immune to
    # the user having closed/reordered tabs) before it's reloaded. This is
    # the fresh-file path, so NO earlier drop exists (show_fresh never tab
    # drops — it must not load the file from disk): the one drop here IS
    # reload_and_relock's, mutation-verified (removing its goto_file fails
    # this index() with ValueError).
    drops = [i for i, text in enumerate(sends) if text == f":tab drop {target}"]
    assert len(drops) == 1
    assert drops[0] < sends.index(":e!")
    refreshed = state.FollowerState.read("$1")
    assert refreshed is not None
    assert refreshed.current_file == str(target)  # following resumes in place
    assert capsys.readouterr().out == ""  # nothing changed for Claude: no notification


def test_hook_post_handoff_keeps_polling_through_unreadable_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\n")
    _register_fake_follower("$1", "%2")
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    calls = {"n": 0}

    def _check(session_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return "interrupt"
        if calls["n"] == 3:
            target.unlink()  # mid-save: file momentarily unreadable → poll again
        if calls["n"] == 4:
            target.write_text("user version\n")  # the actual save lands
        return None  # calls 2: file readable and unchanged → keep waiting

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", side_effect=_check),
        patch("vim_ai_follower.cli.time.sleep") as sleep,
    ):
        assert cli.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert sleep.called  # at least one idle handoff poll happened
    out = json.loads(capsys.readouterr().out)
    assert "SAVED their own version" in out["hookSpecificOutput"]["additionalContext"]
