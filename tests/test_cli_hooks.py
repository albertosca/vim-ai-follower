from __future__ import annotations

import io
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run as _mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import cache, cli, config, control, hooks, keybindings, snapshot, state


def _literal_sends(run_mock: MagicMock) -> list[str]:
    sends = []
    for call in run_mock.call_args_list:
        cmd = call.args[0]
        if cmd[:4] == ["tmux", "send-keys", "-t", "%2"] and "-l" in cmd:
            sends.append(cmd[6])
    return sends


def test_hook_pre_ignores_non_edit_tools() -> None:
    payload: dict[str, object] = {"tool_name": "Bash", "tool_input": {}}
    assert hooks.cmd_hook_pre({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_pre_saves_snapshot_of_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("original\n")
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        assert hooks.cmd_hook_pre({"TMUX_PANE": "%1"}, payload) == 0
    assert snapshot.load("@1", str(target)) == "original\n"


def test_hook_pre_saves_empty_snapshot_for_new_file(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        assert hooks.cmd_hook_pre({"TMUX_PANE": "%1"}, payload) == 0
    assert snapshot.load("@1", str(target)) == ""


def test_hook_pre_noop_without_file_path() -> None:
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {}}
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        assert hooks.cmd_hook_pre({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_pre_noop_without_tmux_env() -> None:
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/f.txt"}}
    assert hooks.cmd_hook_pre({}, payload) == 0


def test_hook_pre_skips_non_code_files_under_code_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"open_policy": "code"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    target = tmp_path / "notes.md"
    target.write_text("some notes\n")
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        assert hooks.cmd_hook_pre({"TMUX_PANE": "%1"}, payload) == 0
    # policy blocked it before a snapshot was ever taken — no snapshot file
    # exists at all (distinct from an empty-file snapshot that was saved)
    assert not snapshot._snapshot_path("@1", str(target), None).exists()


def test_hook_post_noop_when_no_follower_registered(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("content\n")
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_post_edit_noop_without_tmux_env() -> None:
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/f.txt"}}
    assert hooks.cmd_hook_post({}, payload) == 0


def test_hook_post_edit_noop_without_file_path() -> None:
    _register_fake_follower("@1", "%2")
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_post_edit_logs_and_noops_when_file_unreadable(tmp_path: Path) -> None:
    target = tmp_path / "gone.txt"
    _register_fake_follower("@1", "%2")
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_post_read_noop_without_tmux_env() -> None:
    payload: dict[str, object] = {"tool_name": "Read", "tool_input": {"file_path": "/tmp/f.txt"}}
    assert hooks.cmd_hook_post({}, payload) == 0


def test_hook_post_read_noop_when_no_follower_registered(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\n")
    payload: dict[str, object] = {"tool_name": "Read", "tool_input": {"file_path": str(target)}}
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_post_read_noop_without_file_path() -> None:
    _register_fake_follower("@1", "%2")
    payload: dict[str, object] = {"tool_name": "Read", "tool_input": {}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_post_first_open_retypes_from_scratch_instead_of_pasting(tmp_path: Path) -> None:
    # PostToolUse fires after the file is already written, so `:e` on the
    # first view would already show its final content directly, spoiling
    # the "watch it type" effect. Instead the buffer is renamed in place and
    # wiped, then the whole file is retyped from scratch — `:e` never runs.
    target = tmp_path / "f.txt"
    target.write_text("hello\nworld\n")
    snapshot.save("@1", str(target), "hello\n")
    _register_fake_follower("@1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert not any(text.startswith(":e ") for text in sends)
    assert f":file {target}" in sends
    assert ":%d" in sends
    assert "hello" in sends
    assert "world" in sends


def test_hook_post_animates_a_text_edit_on_subsequent_change(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("hello\nworld\n")
    snapshot.save("@1", str(target), "hello\nworld\n")
    _register_fake_follower(
        "@1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )

    target.write_text("hello\nvim ai follower\n")
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert "vim ai follower" in _literal_sends(run)


def test_hook_post_skips_binary_files_on_first_open(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"\x00\x01\x02")
    snapshot.save("@1", str(target), "")
    _register_fake_follower("@1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == [f":tab drop {target}", ":setlocal readonly nomodifiable"]


def test_hook_post_skips_binary_files_on_subsequent_edit(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"\x00\x01\x02")
    snapshot.save("@1", str(target), "")
    _register_fake_follower(
        "@1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == []


def test_hook_post_read_without_offset_does_not_navigate(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\nc\n")
    _register_fake_follower("@1", "%2")

    payload: dict[str, object] = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target)},
    }
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == [f":tab drop {target}", ":setlocal readonly nomodifiable"]


def test_hook_post_read_skips_non_code_files_under_code_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"open_policy": "code"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    target = tmp_path / "notes.md"
    target.write_text("some notes\n")
    _register_fake_follower("@1", "%2")

    payload: dict[str, object] = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target)},
    }
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == []


def test_hook_post_read_navigates_to_file_and_offset(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\nc\n")
    _register_fake_follower("@1", "%2")

    payload: dict[str, object] = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target), "offset": 2},
    }
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

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
    _register_fake_follower("@1", "%2", current_file=str(target))

    payload: dict[str, object] = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target), "offset": 2},
    }
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == [
        f":tab drop {target}",
        ":setlocal readonly nomodifiable",
        ":2",
    ]


def test_hook_post_skips_e_when_file_already_current(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\n")
    _register_fake_follower(
        "@1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )
    snapshot.save("@1", str(target), "a\nb\n")

    target.write_text("a\nX\n")
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert not any(text.startswith(":e ") for text in sends)
    assert "X" in sends


def test_edit_of_untracked_file_is_fresh_and_updates_open_files(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    b.write_text("print('b')\n")
    # b was never shown before (open_files only tracks a) — freshness is
    # driven by open_files membership, not current_file.
    _register_fake_follower("@1", "%2", current_file=str(a), open_files=(str(a),), shown_any=True)

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(b)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert ":tabnew" in sends  # show_fresh's in_new_tab, since shown_any was already True
    assert f":file {b}" in sends  # renamed in place — the show_fresh path, not apply_edit

    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.open_files == (str(a), str(b))
    assert refreshed.shown_any is True


def test_edit_of_tracked_file_uses_apply_edit_even_when_not_current(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("print('a')\n")
    snapshot.save("@1", str(a), "print('a')\n")
    a.write_text("print('a changed')\n")
    # b is the current tab, but the edit targets a — already tracked, so it's
    # not fresh even though it's not the current file.
    _register_fake_follower(
        "@1", "%2", current_file=str(b), open_files=(str(a), str(b)), shown_any=True
    )

    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(a)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

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
        "@1", "%2", current_file=str(b), open_files=(str(a), str(b)), shown_any=True
    )

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(c)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    # a's tab is closed (close_tab: drop + bwipeout, which closes the tab
    # by itself — no :tabclose, see close_tab) BEFORE c's content starts
    # animating (show_fresh's rename-in-place for c).
    close_index = sends.index(f":silent! bwipeout! {a}")
    rename_index = sends.index(f":file {c}")
    assert close_index < rename_index
    assert not any("tabclose" in text for text in sends)

    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.open_files == (str(b), str(c))


def test_eviction_is_a_pure_bookkeeping_noop_for_the_nvim_rpc_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The nvim_rpc backend has no tabs (see Follower protocol docstring) —
    # eviction must still bump open_files, but there is no tab to close.
    config_path = tmp_path / "config.json"
    config_path.write_text('{"max_tabs": 2}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    c = tmp_path / "c.py"
    c.write_text("print('c')\n")
    state.FollowerState.set(
        "@1",
        "nvim_rpc",
        "/tmp/x.sock",
        current_file=str(b),
        open_files=(str(a), str(b)),
        shown_any=True,
    )

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(c)}}
    nvim = MagicMock()
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("pynvim.attach", return_value=nvim),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.open_files == (str(b), str(c))
    # the evicted file (a) is never touched — the tmux backend's close_tab
    # would goto_file + bwipeout it, but this backend has no tab to close
    assert not any(str(a) in str(call) for call in nvim.command.call_args_list)


def test_hook_post_ignores_unrelated_tools() -> None:
    payload: dict[str, object] = {"tool_name": "Bash", "tool_input": {}}
    assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


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


def test_main_speed_and_toggle_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        monkeypatch.setenv("TMUX_PANE", "%1")
        assert cli.main(["start"]) == 0
        assert cli.main(["speed-up"]) == 0
        assert "speed" in capsys.readouterr().out
        assert cli.main(["speed-down"]) == 0
        assert "speed" in capsys.readouterr().out
        assert cli.main(["toggle"]) == 0
        assert "follower muted" in capsys.readouterr().out


def test_main_hook_pre_reads_stdin_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("x\n")
    payload = f'{{"tool_name": "Edit", "tool_input": {{"file_path": "{target}"}}}}'
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
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
        return_value=MagicMock(returncode=0, stdout="@1\n"),
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

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
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
    snapshot.save("@1", str(target), "hello\nworld\n")
    _register_fake_follower(
        "@1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
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
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

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
    _register_fake_follower("@1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    # line "a" fully types (3 checks: i, a, Escape); "interrupt" fires on the
    # very first check of line "b"
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=_interrupt_then_user_saves(target, at_check=4),
        ),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

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
        "@1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )
    snapshot.save("@1", str(target), "old content\n")
    control.save_pending_apply_edit(
        "@1",
        [EditOp(kind="insert", start_line=1, end_line=0, new_lines=("leftover",))],
        0.15,
        file_path=str(target),
    )

    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert "leftover" in sends  # the paused remainder replayed...
    assert sends.index("leftover") < sends.index("new content")  # ...BEFORE the new edit
    assert control.has_pending_animation("@1") is False


def test_hook_post_discards_pending_when_switching_files(tmp_path: Path) -> None:
    old = tmp_path / "old.txt"
    new = tmp_path / "new.txt"
    new.write_text("fresh\n")
    _register_fake_follower("@1", "%2", current_file=str(old))
    control.save_pending_show_fresh("@1", ("leftover",), 0.15, continuation=True)

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(new)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    # show_fresh wipes the buffer anyway — replaying into it would be garbage
    assert "leftover" not in _literal_sends(run)
    assert control.has_pending_animation("@1") is False


def test_hook_post_discards_pending_on_binary_files_too(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\x00\x01\x02")
    _register_fake_follower("@1", "%2", current_file=str(target))
    control.save_pending_show_fresh("@1", ("leftover",), 0.15, continuation=True)

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert "leftover" not in _literal_sends(run)
    assert control.has_pending_animation("@1") is False


def test_hook_post_interrupt_resets_current_file_for_resync(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("line one\nline two\n")
    _register_fake_follower("@1", "%2", current_file=str(target))
    snapshot.save("@1", str(target), "")

    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=_interrupt_then_user_saves(target, at_check=1),
        ),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file is None  # next edit resyncs via a full retype


def test_hook_post_fresh_interrupt_also_resets_current_file(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\n")
    _register_fake_follower("@1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=_interrupt_then_user_saves(target, at_check=1),
        ),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file is None


def test_hooks_are_noops_while_disabled(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("original\n")
    _register_fake_follower(
        "@1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )
    state.FollowerState.update("@1", enabled=False)

    pre_payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.hooks.save_snapshot") as save_mock,
    ):
        assert hooks.cmd_hook_pre({"TMUX_PANE": "%1"}, pre_payload) == 0
    save_mock.assert_not_called()

    target.write_text("changed\n")
    post_payload: dict[str, object] = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
    }
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, post_payload) == 0
    assert _literal_sends(run) == []

    read_payload: dict[str, object] = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target)},
    }
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, read_payload) == 0
    assert _literal_sends(run) == []


def test_auto_open_splits_a_pane_when_policy_always(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    target = tmp_path / "f.txt"
    target.write_text("hello\n")
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}

    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=_mock_tmux_run(other_panes=("%1",)),
    ) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0
        result = state.FollowerState.get("@1")

    splits = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "split-window"]]
    assert splits == [["tmux", "split-window", "-h", "-t", "%1", "-P", "-F", "#{pane_id}", "vim"]]
    assert result is not None
    assert result.target == "%2"
    binds = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "bind-key"]]
    assert len(binds) == len(keybindings._KEYBINDINGS)
    assert "hello" in _literal_sends(run)


def test_auto_open_adopts_existing_vim_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"open_policy": "always", "adopt_existing": true}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    target = tmp_path / "f.txt"
    target.write_text("hello\n")
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}

    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:3] == ["tmux", "list-panes", "-a"]:
            return MagicMock(returncode=0, stdout="%1 zsh\n%7 vim\n")
        if cmd[:4] == ["tmux", "list-panes", "-t", "%1"]:
            return MagicMock(returncode=0, stdout="%1 zsh\n%7 vim\n")
        if cmd[:2] == ["tmux", "display-message"]:
            return MagicMock(returncode=0, stdout="@1\n")
        if cmd[:3] == ["tmux", "list-keys", "-T"]:
            return MagicMock(returncode=1, stdout="")
        return MagicMock(returncode=0, stdout="")

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_run) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0
        result = state.FollowerState.get("@1")

    assert not any(c.args[0][:2] == ["tmux", "split-window"] for c in run.call_args_list)
    assert result is not None
    assert result.target == "%7"
    assert result.adopted is True
    assert result.shown_any is True


def test_auto_open_logs_and_noops_when_the_split_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    target = tmp_path / "f.txt"
    target.write_text("hello\n")
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}

    with (
        patch(
            "vim_ai_follower.tmux.subprocess.run",
            side_effect=_mock_tmux_run(other_panes=("%1",)),
        ),
        patch(
            "vim_ai_follower.hooks.TmuxVimFollower.start",
            side_effect=subprocess.CalledProcessError(1, ["tmux", "split-window"]),
        ),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    # the failed auto-open must not register a follower or crash the hook
    assert state.FollowerState.get("@1") is None


def test_code_policy_skips_non_code_files_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"open_policy": "code"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    target = tmp_path / "notes.md"
    target.write_text("some notes\n")
    _register_fake_follower(
        "@1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == []


def test_manual_policy_never_auto_opens(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("hello\n")
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}

    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=_mock_tmux_run(pane_id="%9", other_panes=("%1",)),
    ) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert not any(c.args[0][:2] == ["tmux", "split-window"] for c in run.call_args_list)
    assert state.FollowerState.get("@1") is None


def test_configure_logging_is_idempotent() -> None:
    hooks._configure_logging()
    handler = hooks.logger.handlers[0]
    hooks._configure_logging()  # a second hook in the same process must not stack handlers
    assert hooks.logger.handlers == [handler]


def test_hook_post_des_interrupt_replays_the_remaining_animation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\n")
    _register_fake_follower("@1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    # interrupt fires at the first check (nothing shown yet); a second S
    # during the hand-off means "discard my typing and put the show back
    # on" — which now REPLAYS the remaining animation from the interrupt
    # point instead of flashing the finished file (live request,
    # 2026-07-15). Nones afterwards let the replay run to completion.
    signals = ["interrupt", "interrupt"] + [None] * 60

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.control.check_signal", side_effect=signals),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert ":e!" not in sends  # never the instant full-file reload
    # the replay typed the file's lines after the des-interrupt
    assert "a" in sends and "b" in sends
    # and relocked (fresh-retype lock) when the replay completed
    assert ":silent! e! | setlocal readonly nomodifiable nopaste" in sends
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file == str(target)  # following resumed in place
    assert capsys.readouterr().out == ""  # nothing changed for Claude: no notification


def test_hook_post_des_interrupt_rebuilds_partial_content_before_replaying(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Unlike the "nothing shown yet" case above, this interrupt fires after
    # two lines were already typed — the des-interrupt must rebuild the
    # buffer to that partial state (rewrite_buffer with non-empty content)
    # before replaying the genuine remainder.
    target = tmp_path / "f.txt"
    target.write_text("a\nb\nc\nd\n")
    _register_fake_follower("@1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    # 6 Nones let "a" and "b" type fully; the first "interrupt" stops "c"
    # before anything of it is sent (completed_count == 2); the second
    # "interrupt" is the handoff loop's first check, firing the des-interrupt
    # immediately. Remaining Nones let the rebuild ("a","b") and the replay
    # of the genuine remainder ("c","d") both run to completion.
    signals = [None] * 6 + ["interrupt", "interrupt"] + [None] * 60

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.control.check_signal", side_effect=signals),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert ":e!" not in sends  # never the instant full-file reload
    # the rebuild retyped the already-shown lines, then the replay typed
    # the genuine remainder
    assert "a" in sends and "b" in sends and "c" in sends and "d" in sends
    assert ":silent! e! | setlocal readonly nomodifiable nopaste" in sends
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file == str(target)
    assert capsys.readouterr().out == ""


def test_hook_post_des_interrupt_without_a_remainder_falls_back_to_reload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # If the interrupt-point remainder is gone (stale/consumed), the old
    # behavior remains: reload the finished file and relock.
    target = tmp_path / "f.txt"
    target.write_text("a\nb\n")
    _register_fake_follower("@1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    original_load = control.load_pending_animation

    def _consume_then_none(window_id: str, base_dir: Path | None = None) -> object:
        original_load(window_id, base_dir)  # consume whatever was saved
        return None

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.control.check_signal", side_effect=["interrupt", "interrupt"]),
        patch(
            "vim_ai_follower.hooks.control.load_pending_animation",
            side_effect=_consume_then_none,
        ),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert ":e!" in sends
    assert ":setlocal readonly nomodifiable" in sends
    assert capsys.readouterr().out == ""


def test_hook_post_handoff_survives_an_unstatable_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The initial mtime read at hand-off time is best-effort (a transient
    # FS hiccup must not crash the hook) — it only guards the "saved
    # unchanged" detection, which then simply never fires.
    target = tmp_path / "f.txt"
    target.write_text("a\nb\n")
    _register_fake_follower("@1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    original_load = control.load_pending_animation

    def _consume_then_none(window_id: str, base_dir: Path | None = None) -> object:
        original_load(window_id, base_dir)
        return None

    # Scope the failure to the target file only (the "unstatable file"),
    # delegating every other Path.stat to the real implementation — a
    # blanket patch would also break unrelated stat calls on the hook path.
    resolved_target = Path(os.path.realpath(str(target)))
    real_stat = Path.stat

    def _stat_raises_for_target(self: Path, *args: object, **kwargs: object) -> object:
        if self == resolved_target:
            raise OSError("boom")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", side_effect=["interrupt", "interrupt"]),
        patch(
            "vim_ai_follower.hooks.control.load_pending_animation",
            side_effect=_consume_then_none,
        ),
        patch("vim_ai_follower.hooks.Path.stat", _stat_raises_for_target),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert capsys.readouterr().out == ""


def test_hook_post_replay_interrupted_again_hands_over_without_a_second_wait(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A third S during the des-interrupt replay hands the buffer to the
    # user and releases the turn — one hand-off wait per hook, no second
    # notification loop.
    target = tmp_path / "f.txt"
    target.write_text("a\nb\nc\nd\n")
    _register_fake_follower("@1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    signals: list[str | None] = ["interrupt", "interrupt", None, None, None, None, "interrupt"]
    signals += [None] * 40

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.control.check_signal", side_effect=signals),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert ":setlocal modifiable nopaste" in sends  # handed over, unlocked
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file is None  # tracking dropped until resync
    assert capsys.readouterr().out == ""  # no notification for the replay stop


def test_hook_post_handoff_keeps_polling_through_unreadable_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\n")
    _register_fake_follower("@1", "%2")
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
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
        patch("vim_ai_follower.hooks.time.sleep") as sleep,
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert sleep.called  # at least one idle handoff poll happened
    out = json.loads(capsys.readouterr().out)
    assert "SAVED their own version" in out["hookSpecificOutput"]["additionalContext"]


def _bounded(check: Callable[..., str | None], limit: int = 50) -> Callable[..., str | None]:
    """Fail loudly instead of hanging when a hand-off wait never releases."""
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] > limit:
            raise AssertionError("hand-off wait never released within the poll budget")
        return check(window_id, base_dir)

    return _check


def test_hook_post_releases_on_a_save_that_kept_claudes_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Live finding (2026-07-15): releasing only on CONTENT change meant a
    # user who saved without editing held Claude's turn forever. A save is
    # a save — identical content must release too, with a lighter
    # notification (there is no user version to build on).
    target = tmp_path / "f.txt"
    target.write_text("hello\nworld\n")
    snapshot.save("@1", str(target), "hello\nworld\n")
    _register_fake_follower(
        "@1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )
    target.write_text("hello\nvim ai follower\n")
    after = target.read_text()

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=_bounded(
                _interrupt_then_user_saves(target, at_check=2, saved_content=after)
            ),
        ),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    out = json.loads(capsys.readouterr().out)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "saved it unchanged" in context
    assert "do not restore" not in context  # the full took-over warning is wrong here


def test_handoff_shows_a_durable_cue_and_periodic_reminders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The 1.5s "Interrupted" popup was too easy to miss (live, 2026-07-15):
    # while the hand-off holds Claude's turn, the follower pane's border
    # title must say how to release it, a popup reminder must re-fire
    # periodically, and both title and border-status must be restored on
    # release no matter how the wait ends.
    target = tmp_path / "f.txt"
    target.write_text("hello\nworld\n")
    snapshot.save("@1", str(target), "hello\nworld\n")
    _register_fake_follower(
        "@1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )
    target.write_text("hello\nvim ai follower\n")

    release_at = 160  # spins past one reminder interval (150 polls) first
    calls = {"n": 0}

    def _interrupt_then_late_save(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == 2:
            return "interrupt"
        if calls["n"] == release_at:
            target.write_text("user version\n")
        return None

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
        patch("vim_ai_follower.control.check_signal", side_effect=_interrupt_then_late_save),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    tmux_calls = [c.args[0] for c in run.call_args_list]
    set_titles = [c for c in tmux_calls if c[:2] == ["tmux", "select-pane"] and "-T" in c]
    assert any("Claude waiting" in c[-1] for c in set_titles)  # cue up
    assert set_titles[-1][-1] == "@1"  # restored to the pre-handoff title afterwards
    border_sets = [c for c in tmux_calls if c[:2] == ["tmux", "set-option"]]
    assert ["tmux", "set-option", "-w", "-t", "%2", "pane-border-status", "top"] in border_sets
    assert ["tmux", "set-option", "-wu", "-t", "%2", "pane-border-status"] in border_sets
    reminder_popups = [
        c.args[0]
        for c in popen.call_args_list
        if any("Claude waiting" in str(a) for a in c.args[0])
    ]
    assert len(reminder_popups) >= 1  # at least one periodic reminder fired


def test_file_paths_are_canonicalized_through_symlinks(tmp_path: Path) -> None:
    # macOS: /tmp is a symlink to /private/tmp, and Vim resolves buffer
    # names to the real path — a :tab drop with the symlinked spelling
    # misses the existing buffer and opens a duplicate tab. Every path
    # entering the hooks must be canonical.
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)
    target = link_dir / "f.py"
    target.write_text("x = 1\n")
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    assert hooks._file_path(payload) == str(real_dir / "f.py")


def test_edit_skips_while_another_process_animates(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("new content\n")
    file_path = os.path.realpath(str(target))
    _register_fake_follower("@1", "%2", open_files=(file_path,), shown_any=True)
    control.mark_animating("@1")  # this test process's live PID: a live foreign writer
    try:
        with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
            exit_code = hooks.cmd_hook_post(
                {"TMUX_PANE": "%1"},
                {"tool_name": "Edit", "tool_input": {"file_path": str(target)}},
            )
    finally:
        control.clear_animating("@1")
    assert exit_code == 0
    sent = [
        c for c in (call.args[0] for call in run.call_args_list) if c[:2] == ["tmux", "send-keys"]
    ]
    assert sent == []  # nothing animated into the pane
    current = state.FollowerState.read("@1")
    assert current is not None
    assert file_path not in current.open_files  # next touch resyncs fresh


def test_read_skips_while_another_process_animates(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("content\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    control.mark_animating("@1")
    try:
        with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
            exit_code = hooks.cmd_hook_post(
                {"TMUX_PANE": "%1"},
                {"tool_name": "Read", "tool_input": {"file_path": str(target)}},
            )
    finally:
        control.clear_animating("@1")
    assert exit_code == 0
    sent = [
        c for c in (call.args[0] for call in run.call_args_list) if c[:2] == ["tmux", "send-keys"]
    ]
    assert sent == []
    current = state.FollowerState.read("@1")
    assert current is not None
    assert current.open_files == ()  # untouched


def test_skip_preserves_the_other_writers_pending_animation(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("new content\n")
    _register_fake_follower("@1", "%2", open_files=(os.path.realpath(str(target)),), shown_any=True)
    control.save_pending_apply_edit("@1", [], 0.0, file_path=os.path.realpath(str(target)))
    control.mark_animating("@1")
    try:
        with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
            hooks.cmd_hook_post(
                {"TMUX_PANE": "%1"},
                {"tool_name": "Edit", "tool_input": {"file_path": str(target)}},
            )
    finally:
        control.clear_animating("@1")
    assert control.has_pending_animation("@1")  # never consumed by the skipped hook


def test_dead_marker_does_not_block_the_edit(tmp_path: Path) -> None:
    # A crashed writer's marker (dead PID) must not suppress animation:
    # animating_state already returns None for it, so the edit proceeds.
    target = tmp_path / "a.py"
    target.write_text("new content\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    marker = cache.CACHE_DIR / "@1.animating"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("99999999 running")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        hooks.cmd_hook_post(
            {"TMUX_PANE": "%1"},
            {"tool_name": "Edit", "tool_input": {"file_path": str(target)}},
        )
    sent = [
        c for c in (call.args[0] for call in run.call_args_list) if c[:2] == ["tmux", "send-keys"]
    ]
    assert sent != []  # animation ran


def test_edit_skip_survives_state_cleared_out_from_under_the_marker(tmp_path: Path) -> None:
    # .animating and .pane have decoupled lifecycles: a concurrent cmd_stop
    # can clear this window's FollowerState while another live process's
    # .animating marker survives (cmd_stop never clears its own marker), or
    # the animator can re-mark right after a stop clears it. Either way the
    # guard must still skip cleanly with no state to update and no crash.
    target = tmp_path / "a.py"
    target.write_text("new content\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    control.mark_animating("@1")
    state.FollowerState.clear("@1")  # simulates a concurrent cmd_stop
    try:
        with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
            exit_code = hooks.cmd_hook_post(
                {"TMUX_PANE": "%1"},
                {"tool_name": "Edit", "tool_input": {"file_path": str(target)}},
            )
    finally:
        control.clear_animating("@1")
    assert exit_code == 0
    sent = [
        c for c in (call.args[0] for call in run.call_args_list) if c[:2] == ["tmux", "send-keys"]
    ]
    assert sent == []
