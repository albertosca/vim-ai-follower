from __future__ import annotations

import dataclasses
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch
from unittest.mock import call as mock_call

import pytest
from helpers import (
    call_spelled,
    define_line,
    fake_nvim,
    goto_spelled,
    landed_line,
    rename_spelled,
    resolve_typed_paths,
    route_exec_lua,
    shows_the_buffer,
    wipe_spelled,
)
from helpers import make_mock_tmux_run as _mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import (
    cache,
    cli,
    commands,
    config,
    control,
    hooks,
    keybindings,
    snapshot,
    state,
    writer_cue,
)
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends import NavigationFailed, nvim_connect
from vim_ai_follower.backends.nvim import _FIND_BUFFER_LUA
from vim_ai_follower.backends.nvim_connect import NvimNeverListened
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.session import Session

# The readonly completion relock of a dedicated follower: the scoped swap
# answer around a silent re-read, then the lock (s:relock).
_READONLY_RELOCK = call_spelled("relock", 1, 0)
# The discarding reload (reload_and_relock's / reload_from_disk's `silent
# edit!`, s:reload) of a dedicated follower.
_RELOAD_DISCARDING = call_spelled("reload", 1, 0)
# hand_over's unlock of a dedicated Vim, giving the user's global 'paste'
# back from its record (s:hand_over).
_HAND_OVER = call_spelled("hand_over", 0)


def _goto(path: object) -> str:
    """The call goto_file sends for `path` (helpers.goto_spelled: spelled
    out there, never imported from tmux_vim)."""
    return goto_spelled(path)


def _rename(path: object, *, in_new_tab: bool = False) -> str:
    """show_fresh's rename-in-place line (helpers.rename_spelled)."""
    return rename_spelled(path, in_new_tab=in_new_tab)


# ensure_showing's clean-only disk re-read (s:reload) in a dedicated Vim,
# spelled out for the same reason as _goto.
_RELOAD_IF_CLEAN = call_spelled("reload", 0, 0)


def _literal_sends(run_mock: MagicMock) -> list[str]:
    sends = []
    for call in run_mock.call_args_list:
        cmd = call.args[0]
        if cmd[:4] == ["tmux", "send-keys", "-t", "%2"] and "-l" in cmd:
            sends.append(resolve_typed_paths(cmd[6]))
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
    assert _rename(target) in sends
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

    assert _literal_sends(run) == [
        define_line(),
        _goto(target),
        landed_line(),
        _RELOAD_IF_CLEAN,
        ":setlocal readonly nomodifiable|redraw|echo ''",
    ]


def test_hook_post_shows_a_rewritten_binary_on_subsequent_edit(tmp_path: Path) -> None:
    # Binaries are never animated, but an edit to one already open must still
    # navigate there and re-read it (clean buffer) — the old `== []` pin dated
    # from before the reload-if-clean existed and left the stale bytes showing.
    target = tmp_path / "f.bin"
    target.write_bytes(b"\x00\x01\x02")
    snapshot.save("@1", str(target), "")
    _register_fake_follower(
        "@1", "%2", current_file=str(target), open_files=(str(target),), shown_any=True
    )

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert _literal_sends(run) == [
        define_line(),
        _goto(target),
        landed_line(),
        _RELOAD_IF_CLEAN,
        ":setlocal readonly nomodifiable|redraw|echo ''",
    ]


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

    assert _literal_sends(run) == [
        define_line(),
        _goto(target),
        landed_line(),
        _RELOAD_IF_CLEAN,
        ":setlocal readonly nomodifiable|redraw|echo ''",
    ]


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
        define_line(),
        _goto(target),
        landed_line(),
        _RELOAD_IF_CLEAN,
        ":setlocal readonly nomodifiable|redraw|echo ''",
        ":exe 2|redraw|echo ''",
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
        define_line(),
        _goto(target),
        landed_line(),
        _RELOAD_IF_CLEAN,
        ":setlocal readonly nomodifiable|redraw|echo ''",
        ":exe 2|redraw|echo ''",
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
    # show_fresh's in_new_tab (shown_any was already True), on its guarded
    # rename line — the show_fresh path, not apply_edit
    assert _rename(b, in_new_tab=True) in sends

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
    # The mocked tmux has no Vim to answer the base probe; a's buffer holding
    # the snapshot is this test's premise (tests/test_hooks_base_mismatch.py
    # covers the other answer).
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch.object(TmuxVimFollower, "probe_buffer", return_value="holds"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    # apply_edit's goto_file preamble navigates to a's tab — the backend's
    # job, not cli.py's — then the diff is applied in place; show_fresh's
    # rename-in-place (":file <path>") never runs.
    assert _goto(a) in sends
    assert not any("'rename'" in text for text in sends)
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
    # a's tab is closed BEFORE c's content starts animating (show_fresh's
    # rename-in-place for c). close_tab resolves a's BUFFER NUMBER and wipes
    # that — wiping by name is a silent no-op for any path Vim reads as a
    # buffer-name pattern — and the wipe closes the tab by itself, so there
    # is no :tabclose (see close_tab).
    close_index = sends.index(wipe_spelled(a))
    rename_index = sends.index(_rename(c, in_new_tab=True))
    assert close_index < rename_index
    assert not any("tabclose" in text for text in sends)

    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.open_files == (str(b), str(c))


def test_eviction_wipes_the_buffer_for_the_nvim_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The nvim backend has buffers, not tabs (see Follower protocol docstring),
    # but close_tab wipes the buffer generically — eviction is no longer
    # tmux-only (Task 6).
    config_path = tmp_path / "config.json"
    config_path.write_text('{"max_tabs": 2}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    c = tmp_path / "c.py"
    c.write_text("print('c')\n")
    state.FollowerState.set(
        "@1",
        "nvim",
        "/tmp/x.sock",
        current_file=str(b),
        open_files=(str(a), str(b)),
        shown_any=True,
    )

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(c)}}
    nvim = MagicMock()
    # buffer 7 is what the lookup resolves str(a) to; the naming answers the path itself
    route_exec_lua(nvim, buffer=7, display_name=str(c))
    shows_the_buffer(nvim, number=7)  # goto_file switches to it, opens no tab
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.open_files == (str(b), str(c))
    # close_tab looks the evicted file (a) up by walking the buffer list
    # (never bufnr(), which pattern-matches) then wipes it by number.
    nvim.exec_lua.assert_any_call(_FIND_BUFFER_LUA, str(a))
    nvim.command.assert_any_call("silent! bwipeout! 7")


def test_touch_and_evict_closes_the_evicted_tab_on_any_follower(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Generic eviction (Task 6): _touch_and_evict must call close_tab on
    # whatever follower it's given — not just TmuxVimFollower — proven here
    # with a bare mock that is deliberately NOT a TmuxVimFollower instance.
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    state.FollowerState.set(
        "@1", "nvim", "/tmp/x.sock", open_files=("/tmp/a.py", "/tmp/b.py"), shown_any=True
    )
    current = state.FollowerState.read("@1")
    assert current is not None

    follower = MagicMock()
    hooks._touch_and_evict("@1", follower, current, "/tmp/c.py", max_tabs=2)

    follower.close_tab.assert_called_once_with("/tmp/a.py")
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.open_files == ("/tmp/b.py", "/tmp/c.py")


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
        # status now leads with the identity it is answering about, so that
        # "no follower" can never again be read as "no follower anywhere".
        assert "active for @1 (tmux), backend tmux (%2)" in capsys.readouterr().out
        assert cli.main(["stop"]) == 0
        assert cli.main(["status"]) == 0
        assert "no follower for @1 (tmux)" in capsys.readouterr().out


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


def test_main_hook_pre_malformed_stdin_exits_zero_and_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BACKLOG "Hooks have no top-level catch": malformed stdin JSON at
    cli.py's json.loads used to propagate as an uncaught exception, exiting
    1 with a traceback that Claude Code reports as a hook error. The
    top-level catch scoped to `hook pre|post` must swallow it, exit 0 and
    leave the traceback in hook.log instead."""
    monkeypatch.setattr("sys.stdin", io.StringIO("not valid json"))
    assert cli.main(["hook", "pre"]) == 0
    log_text = hooks.LOG_PATH.read_text() if hooks.LOG_PATH.exists() else ""
    assert "hook pre crashed" in log_text
    assert "JSONDecodeError" in log_text


def test_main_hook_post_exception_exits_zero_and_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception raised inside cmd_hook_post itself (not just malformed
    stdin) must be caught the same way: exit 0, traceback in hook.log."""
    monkeypatch.setattr("sys.stdin", io.StringIO('{"tool_name": "Bash"}'))

    def _boom(env: dict[str, str], payload: dict[str, object]) -> int:
        raise RuntimeError("injected hook boom")

    monkeypatch.setattr(hooks, "cmd_hook_post", _boom)
    assert cli.main(["hook", "post"]) == 0
    log_text = hooks.LOG_PATH.read_text() if hooks.LOG_PATH.exists() else ""
    assert "hook post crashed" in log_text
    assert "injected hook boom" in log_text
    assert "Traceback" in log_text


def test_main_non_hook_command_still_raises_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catch-all is scoped to `hook pre|post` only: every other
    subcommand must keep failing loudly, never silently swallowed to 0."""

    def _boom(env: dict[str, str]) -> int:
        raise RuntimeError("injected status boom")

    monkeypatch.setattr(commands, "cmd_status", _boom)
    with pytest.raises(RuntimeError, match="injected status boom"):
        cli.main(["status"])


def test_run_hook_configure_logging_failure_still_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix round 1 (review Critical): _configure_logging() used to run
    BEFORE run_hook's try, so an unwritable cache dir or a full disk (the
    FileHandler open failing) escaped uncaught — and did so on EVERY hook
    invocation, since each hook is a fresh process re-running it. It must
    now be inside the same try as everything else."""

    def _boom(*args: object, **kwargs: object) -> logging.FileHandler:
        raise OSError("disk full")

    monkeypatch.setattr(logging, "FileHandler", _boom)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"tool_name": "Bash"}'))
    assert cli.main(["hook", "post"]) == 0


def test_run_hook_stdin_read_failure_still_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix round 1 (review Critical): cli.py used to call `sys.stdin.read()`
    as an ARGUMENT to run_hook, evaluated before the call and therefore
    outside its try — a UnicodeDecodeError from non-UTF-8 stdin (or any
    other I/O error) escaped. run_hook must take the stream itself and read
    it inside its own protected path."""

    class _BoomStdin:
        def read(self) -> str:
            raise UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")

    monkeypatch.setattr("sys.stdin", _BoomStdin())
    assert cli.main(["hook", "pre"]) == 0
    log_text = hooks.LOG_PATH.read_text() if hooks.LOG_PATH.exists() else ""
    assert "hook pre crashed" in log_text
    assert "UnicodeDecodeError" in log_text


def test_run_hook_real_subprocess_non_utf8_stdin_exits_zero(tmp_path: Path) -> None:
    """The same non-UTF-8-stdin scenario as above, but through the REAL CLI
    as a real subprocess with real bytes on stdin — proving the in-process
    fake-.read() test above matches what Python's own text-mode stdin does
    with invalid bytes, not just what a hand-written stub does. HOME is
    redirected to an isolated tmp dir so this can never touch the real
    ~/.cache/claude-vim-follower on the machine running the suite."""
    result = subprocess.run(
        [sys.executable, "-m", "vim_ai_follower.cli", "hook", "pre"],
        input=b"\xff\xfe not valid utf-8 \x80\x81",
        capture_output=True,
        env={**os.environ, "HOME": str(tmp_path)},
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_run_hook_logging_the_crash_itself_also_fails_still_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix round 1 (review Critical): the except handler's own attempt to
    log the crash is wrapped in a second, unconditional try — if hook.log
    itself is what's broken, logging the ORIGINAL crash can raise too, and
    that second failure must not escape either. Both the dispatch AND the
    logging of its failure raise here."""
    monkeypatch.setattr("sys.stdin", io.StringIO('{"tool_name": "Bash"}'))

    def _boom_dispatch(env: dict[str, str], payload: dict[str, object]) -> int:
        raise RuntimeError("dispatch boom")

    def _boom_log(*args: object, **kwargs: object) -> None:
        raise OSError("log write also failed")

    monkeypatch.setattr(hooks, "cmd_hook_post", _boom_dispatch)
    monkeypatch.setattr(hooks.logger, "exception", _boom_log)
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
    # fires — the ex-command text gets typed but never committed. The buffer
    # holding the snapshot is the premise (no Vim here answers the probe).
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch.object(TmuxVimFollower, "probe_buffer", return_value="holds"),
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=_interrupt_then_user_saves(target, at_check=2),
        ),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    # No relock of any form (the synced call or a plain line).
    assert not any("nomodifiable" in text or "'relock'" in text for text in sends)
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
    # line "a" fully types (6 checks: i, a, Escape, Escape, C-\, C-n);
    # "interrupt" fires on the very first check of line "b"
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=_interrupt_then_user_saves(target, at_check=7),
        ),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    # No completion relock after an interrupt: show_fresh's relock is the SYNCED
    # form (a disk re-read, then the readonly lock), so match its tail.
    assert _READONLY_RELOCK not in sends
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


def test_auto_open_selects_nvim_backend_and_persists_launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"open_policy": "always", "backend": "nvim"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    target = tmp_path / "f.py"
    target.write_text("print(1)\n")
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    nvim = MagicMock()
    route_exec_lua(nvim, buffer=-1, display_name=str(target))

    with (
        patch(
            "vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run(other_panes=("%1",))
        ),
        patch(
            "vim_ai_follower.hooks.resolve_nvim_target",
            return_value=("/tmp/nvim.sock", True),
        ) as resolve,
        # The follower's own connection (show_fresh) is mocked out — this test
        # only asserts the backend selection and persisted state.
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    resolve.assert_called_once_with("%1", "@1", adopt=False)
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.backend == "nvim"
    assert result.target == "/tmp/nvim.sock"
    assert result.adopted is False


def test_auto_open_nvim_resolve_failure_logs_and_noops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"open_policy": "always", "backend": "nvim"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    target = tmp_path / "f.py"
    target.write_text("print(1)\n")
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}

    with (
        patch(
            "vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run(other_panes=("%1",))
        ),
        patch(
            "vim_ai_follower.hooks.resolve_nvim_target",
            side_effect=subprocess.CalledProcessError(1, ["tmux", "split-window"]),
        ),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    # A failed nvim auto-open registers nothing and never crashes the hook.
    assert state.FollowerState.read("@1") is None


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
    # never the instant full-file reload (reload_and_relock's `silent edit!`)
    assert _RELOAD_DISCARDING not in sends
    # the replay typed the file's lines after the des-interrupt
    assert "a" in sends and "b" in sends
    # and relocked (fresh-retype lock) when the replay completed
    # the relock: the scoped swap answer, a silent re-read, the readonly lock
    assert _READONLY_RELOCK in sends
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file == str(target)  # following resumed in place
    assert capsys.readouterr().out == ""  # nothing changed for Claude: no notification


def test_reconstruct_partial_fresh_preserves_consecutive_trailing_blanks() -> None:
    # The reconstructed partial is `.splitlines()`'d again downstream (by
    # rewrite_buffer and the interrupt notification), so the round-trip must be
    # lossless for trailing blank lines. A "\n".join would collapse them:
    # ['a','',''] -> "a\n\n" -> ['a',''] drops one. Terminating each line keeps
    # them: ['a','',''] -> "a\n\n\n" -> ['a','',''].
    content = "a\n\n\nb\nc\n"
    assert content.splitlines() == ["a", "", "", "b", "c"]

    # completed_count spanning both trailing blanks reconstructs them intact.
    partial = hooks._reconstruct_partial_fresh(content, 3)
    assert partial == "a\n\n\n"
    assert partial.splitlines() == ["a", "", ""]  # lossless round-trip

    # nothing shown yet -> empty string -> no lines (unchanged from before).
    assert hooks._reconstruct_partial_fresh(content, 0) == ""
    assert hooks._reconstruct_partial_fresh(content, 0).splitlines() == []


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
    # never the instant full-file reload (reload_and_relock's `silent edit!`)
    assert _RELOAD_DISCARDING not in sends
    # the rebuild retyped the already-shown lines, then the replay typed
    # the genuine remainder
    assert "a" in sends and "b" in sends and "c" in sends and "d" in sends
    # the relock: the scoped swap answer, a silent re-read, the readonly lock
    assert _READONLY_RELOCK in sends
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
    # the reload of the finished file, under the scoped (E)dit-anyway answer
    assert _RELOAD_DISCARDING in sends
    assert ":setlocal readonly nomodifiable|redraw|echo ''" in sends
    assert capsys.readouterr().out == ""


def test_des_interrupt_routes_through_the_backend_follower_for_nvim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The des-interrupt branch must build the follower from the window's
    # backend, not a hardcoded TmuxVimFollower. With backend "nvim" it must
    # reach the NvimFollower (constructed via get_follower with the socket
    # target) — mutation-catching: the old `TmuxVimFollower(pane_id=...)` would
    # get the socket as a pane id and never route to nvim at all.
    target = tmp_path / "f.txt"
    target.write_text("a\nb\n")
    state.FollowerState.set(
        "@1",
        "nvim",
        "/tmp/x.sock",
        current_file=str(target),
        open_files=(str(target),),
        shown_any=True,
    )
    current = state.FollowerState.read("@1")
    assert current is not None
    control.save_pending_show_fresh("@1", ("b",), 0.0, continuation=True, file_path=str(target))

    fake = MagicMock()
    fake.rewrite_buffer.return_value = AnimationResult("completed", 1)
    fake.resume.return_value = AnimationResult("completed", 1)
    captured: dict[str, object] = {}

    def _fake_get_follower(backend: str, follower_target: str, **kwargs: object) -> MagicMock:
        captured["backend"] = backend
        captured["target"] = follower_target
        return fake

    monkeypatch.setattr(hooks, "get_follower", _fake_get_follower)
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hooks, "show_popup", lambda *a, **k: None)

    with (
        patch("vim_ai_follower.control.check_signal", side_effect=["interrupt"] + [None] * 5),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        hooks._await_user_handoff(
            current, Session(window_id="@1", origin="%1", in_tmux=True), str(target), "a\nb\n", "a"
        )

    assert captured["backend"] == "nvim"
    assert captured["target"] == "/tmp/x.sock"
    fake.rewrite_buffer.assert_called_once_with(str(target), "a")
    fake.resume.assert_called_once()
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file == str(target)


def test_pace0_consume_routes_resume_to_the_nvim_follower(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pace-0 pending-consume (hooks.py:439) must call resume on whatever
    # backend follower it built — not assert-isinstance TmuxVimFollower. With
    # backend "nvim" and a persisted pending, the old assert would have raised
    # AssertionError; the new path just calls follower.resume at pace 0.
    from vim_ai_follower.diff import EditOp

    target = tmp_path / "f.txt"
    target.write_text("a\nB\n")
    state.FollowerState.set(
        "@1",
        "nvim",
        "/tmp/x.sock",
        current_file=str(target),
        open_files=(str(target),),
        shown_any=True,
    )
    snapshot.save("@1", str(target), "a\nb\n")
    control.save_pending_apply_edit(
        "@1",
        [EditOp(kind="replace", start_line=2, end_line=2, new_lines=("B",))],
        0.03,
        file_path=str(target),
    )

    fake = MagicMock()
    fake.apply_edit.return_value = AnimationResult("completed", 1)
    # A real outcome: a bare MagicMock's is neither, and a catch-up that is not
    # "completed" is an S's hand-off, which would wait for a save forever.
    fake.resume.return_value = AnimationResult("completed", 1)
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}

    with (
        patch("vim_ai_follower.hooks.get_follower", return_value=fake),
        patch("pynvim.attach", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    fake.resume.assert_called_once()
    consumed = fake.resume.call_args.args[0]
    assert consumed.pace_seconds == 0.0  # the catch-up is forced silent
    # the pace-0 consume runs on the LIVE interrupted buffer, which still
    # carries show_fresh's trailing seed blank — provenance passed explicitly
    assert fake.resume.call_args.kwargs.get("seeded") is True
    assert list(consumed.ops) == [
        EditOp(kind="replace", start_line=2, end_line=2, new_lines=("B",))
    ]


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


def test_hook_post_replay_interrupted_again_hands_over_and_waits_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A third S during the des-interrupt replay hands the buffer to the user
    # and RE-ENTERS the hand-off wait. It used to return there ("one hand-off
    # wait per hook"), so a fourth S reached nobody and the buffer stayed
    # partial (live, 2026-09-16); now the user's save still releases the turn.
    # tests/test_hooks_handoff_loop.py covers the cycling itself.
    target = tmp_path / "f.txt"
    target.write_text("a\nb\nc\nd\n")
    _register_fake_follower("@1", "%2")

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] in {1, 2, 7}:  # interrupt, des-interrupt, stop the replay
            return "interrupt"
        if calls["n"] == 12:  # a few polls into the SECOND wait, the user saves
            target.write_text("the user's own version\n")
        return None

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.control.check_signal", side_effect=_bounded(_check)),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert _HAND_OVER in sends  # handed over, unlocked
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file is None  # tracking dropped until resync
    out = json.loads(capsys.readouterr().out)
    assert "SAVED their own version" in out["hookSpecificOutput"]["additionalContext"]


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


@pytest.mark.parametrize("already_open", [True, False], ids=["diff", "fresh-retype"])
def test_handoff_release_restores_the_title_from_before_the_animation(
    tmp_path: Path, already_open: bool
) -> None:
    # The animation raises "Writing..." on one surface and the hand-off wait
    # used to open a SECOND one, which saved "Writing..." as the title to
    # restore — so after the user's save the border said "Writing..." with
    # nothing writing (measured while recording the demo, 2026-09-23). The
    # shared mock answers every display-message with the window id, which
    # hid it; this fake remembers the title it was given.
    target = tmp_path / "f.txt"
    target.write_text("hello\nworld\n")
    snapshot.save("@1", str(target), "hello\nworld\n")
    _register_fake_follower(
        "@1",
        "%2",
        current_file=str(target),
        open_files=(str(target),) if already_open else (),
        shown_any=True,
    )
    target.write_text("hello\nvim ai follower\n")
    base = _mock_tmux_run()
    title = {"%2": "before"}

    def _stateful_title(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["tmux", "select-pane"] and "-T" in cmd:
            title[cmd[cmd.index("-t") + 1]] = cmd[-1]
        if cmd[:2] == ["tmux", "display-message"] and cmd[-1] == "#{pane_title}":
            return MagicMock(returncode=0, stdout=title[cmd[cmd.index("-t") + 1]] + "\n")
        return base(cmd, **kwargs)

    calls = {"n": 0}

    def _interrupt_then_save(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == 2:
            return "interrupt"
        if calls["n"] == 5:
            target.write_text("user version\n")
        return None

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_stateful_title),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
        patch("vim_ai_follower.control.check_signal", side_effect=_interrupt_then_save),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert title["%2"] == "before"


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
    # The tab is still there, so it stays an eviction candidate; only its
    # buffer is marked out of sync, which is what forces the fresh retype.
    assert current.open_files == (file_path,)
    assert current.stale_files == (file_path,)


def test_repeated_busy_slot_collisions_mark_a_file_stale_once(tmp_path: Path) -> None:
    """Two lost races on the same file are one mark, not two.

    A duplicate would survive eviction's subset filter no better than a
    single entry, but it would make stale_files grow without bound in the
    window that hits this most — the many-parallel-writers one."""
    target = tmp_path / "a.py"
    target.write_text("new content\n")
    file_path = os.path.realpath(str(target))
    _register_fake_follower("@1", "%2", open_files=(file_path,), shown_any=True)
    control.mark_animating("@1")
    try:
        with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
            for _ in range(2):
                assert (
                    hooks.cmd_hook_post(
                        {"TMUX_PANE": "%1"},
                        {"tool_name": "Edit", "tool_input": {"file_path": str(target)}},
                    )
                    == 0
                )
    finally:
        control.clear_animating("@1")
    current = state.FollowerState.read("@1")
    assert current is not None
    assert current.stale_files == (file_path,)


def test_busy_slot_collision_on_an_untracked_file_tracks_nothing(tmp_path: Path) -> None:
    """The no-op case the old drop-from-open_files also handled.

    An untracked file has no tab and therefore no buffer that could be out
    of sync — marking it stale would both be meaningless and violate the
    stale-is-a-subset-of-open invariant."""
    target = tmp_path / "a.py"
    target.write_text("new content\n")
    other = os.path.realpath(str(tmp_path / "b.py"))
    _register_fake_follower("@1", "%2", open_files=(other,), shown_any=True)
    control.mark_animating("@1")
    try:
        with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
            assert (
                hooks.cmd_hook_post(
                    {"TMUX_PANE": "%1"},
                    {"tool_name": "Edit", "tool_input": {"file_path": str(target)}},
                )
                == 0
            )
    finally:
        control.clear_animating("@1")
    current = state.FollowerState.read("@1")
    assert current is not None
    assert current.open_files == (other,)
    assert current.stale_files == ()


def test_stale_file_is_retyped_fresh_and_the_mark_clears_on_completion(tmp_path: Path) -> None:
    """A tracked-but-stale file must take show_fresh, not apply_edit.

    The snapshot on disk would make a perfectly valid-looking diff, which is
    exactly the trap: the buffer never received the edit the snapshot was
    taken around, so applying the diff onto it produces garbage. The rename
    in place (":file <path>") is show_fresh's signature; apply_edit never
    emits one and goes through the goto_file preamble instead."""
    target = tmp_path / "a.py"
    target.write_text("print('after')\n")
    file_path = os.path.realpath(str(target))
    snapshot.save("@1", file_path, "print('before')\n")
    _register_fake_follower(
        "@1", "%2", current_file=file_path, open_files=(file_path,), shown_any=True
    )
    state.FollowerState.update("@1", stale_files=(file_path,))

    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = _literal_sends(run)
    assert _rename(file_path, in_new_tab=True) in sends  # show_fresh's rename
    assert _goto(file_path) not in sends  # ...not apply_edit's navigation
    assert "print('after')" in sends

    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.stale_files == ()  # the retype finished: back in sync
    assert refreshed.open_files == (file_path,)


def test_an_interrupted_retype_leaves_the_file_stale(tmp_path: Path) -> None:
    """The mirror of the test above, and the reason the clear is not simply
    unconditional: an interrupt leaves a half-typed buffer, so the file is
    still out of sync and the NEXT touch must retype it again."""
    target = tmp_path / "a.py"
    target.write_text("alpha\nbeta\n")
    file_path = os.path.realpath(str(target))
    _register_fake_follower(
        "@1", "%2", current_file=file_path, open_files=(file_path,), shown_any=True
    )
    state.FollowerState.update("@1", stale_files=(file_path,))

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=_interrupt_then_user_saves(target, at_check=7),
        ),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.stale_files == (file_path,)


def test_eviction_drops_the_victim_from_open_and_stale_alike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale entry naming a closed tab is exactly the dangling shape that
    would grow forever, so eviction has to take both tuples."""
    config_path = tmp_path / "config.json"
    config_path.write_text('{"max_tabs": 2}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    a = os.path.realpath(str(tmp_path / "a.py"))
    b = os.path.realpath(str(tmp_path / "b.py"))
    c = tmp_path / "c.py"
    c.write_text("print('c')\n")
    _register_fake_follower("@1", "%2", current_file=b, open_files=(a, b), shown_any=True)
    state.FollowerState.update("@1", stale_files=(a,))

    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(c)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.open_files == (b, os.path.realpath(str(c)))
    assert refreshed.stale_files == ()  # a's tab is closed; its mark went with it


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


_CONCURRENT_EDIT = """
import sys, time
from pathlib import Path
from vim_ai_follower import cache, control
cache.CACHE_DIR = Path(sys.argv[1])
print(control.try_acquire_animating("@1"), flush=True)
time.sleep(30)
"""


def test_a_read_holds_the_slot_while_it_navigates(tmp_path: Path) -> None:
    """Final review of backlog-sweep-3, M2: the Read used to check the slot
    and then navigate, and its navigation is several sends (define, goto,
    Escape pair, landed, a wait of up to 10 s, reload, lock). An Edit hook
    that took the slot in between had its insert mode ended by the Read's
    Escape pair, and its remaining text typed as normal-mode commands. Here
    another process tries to take the slot exactly there (when the Read asks
    whether an Edit of the file is in flight, its first step after the
    check): it must find the slot held by the Read, which still navigates and
    releases the slot on its way out."""
    target = tmp_path / "a.py"
    target.write_text("content\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    processes: list[subprocess.Popen[str]] = []
    answers: list[str] = []
    real_in_flight = hooks._edit_in_flight

    def concurrent_edit_then_check(window_id: str, file_path: str, reader: str | None) -> bool:
        process = subprocess.Popen(
            [sys.executable, "-c", _CONCURRENT_EDIT, str(cache.CACHE_DIR)],
            stdout=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        assert process.stdout is not None
        answers.append(process.stdout.readline().strip())
        return real_in_flight(window_id, file_path, reader)

    try:
        with (
            patch.object(hooks, "_edit_in_flight", side_effect=concurrent_edit_then_check),
            patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        ):
            exit_code = hooks.cmd_hook_post(
                {"TMUX_PANE": "%1"},
                {"tool_name": "Read", "tool_input": {"file_path": str(target)}},
            )
    finally:
        for process in processes:
            process.kill()
            process.wait()
    assert exit_code == 0
    assert answers == ["False"]  # the concurrent Edit found the slot taken
    sent = [
        command
        for command in (call.args[0] for call in run.call_args_list)
        if command[:2] == ["tmux", "send-keys"]
    ]
    assert sent != []  # the Read did navigate
    assert control.animating_state("@1") is None  # and released the slot


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


def _edit_payload(target: Path, **identity: str) -> dict[str, object]:
    return {"tool_name": "Edit", "tool_input": {"file_path": str(target)}, **identity}


def test_single_writer_leaves_the_border_neutral(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("x\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _edit_payload(target, session_id="$1"))
    # One writer: never TINTED. The completion refresh may issue a clear
    # (reset-to-default) border call — that is not a tint, so filter on the
    # fg= payload rather than any mention of the option.
    tint_calls = [
        c.args[0]
        for c in run.call_args_list
        if "pane-border-style" in c.args[0] and any(str(a).startswith("fg=") for a in c.args[0])
    ]
    assert tint_calls == []
    assert state.FollowerState.read("@1").writers == ("$1",)  # type: ignore[union-attr]


def test_second_distinct_writer_tints_the_border_with_its_color_and_label(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("x\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _edit_payload(target, session_id="$1"))
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        hooks.cmd_hook_post(
            {"TMUX_PANE": "%1"},
            _edit_payload(target, session_id="$1", agent_id="a9", agent_type="code-reviewer"),
        )
    calls = [c.args[0] for c in run.call_args_list]
    # a9 is the 2nd writer -> PALETTE[1]; border set on the follower pane %2
    assert [
        "tmux",
        "set-option",
        "-p",
        "-t",
        "%2",
        "pane-border-style",
        f"fg={writer_cue.PALETTE[1]}",
    ] in calls
    assert ["tmux", "select-pane", "-t", "%2", "-T", "code-reviewer"] in calls
    result = state.FollowerState.read("@1")
    assert result.writers == ("$1", "a9")  # type: ignore[union-attr]
    assert result.writer_labels == ("session:$1", "code-reviewer")  # type: ignore[union-attr]


def test_skipped_concurrent_writer_is_registered_but_does_not_tint(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("x\n")
    _register_fake_follower("@1", "%2", open_files=(os.path.realpath(str(target)),), shown_any=True)
    control.mark_animating("@1")  # another live writer owns the pane
    try:
        with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
            hooks.cmd_hook_post(
                {"TMUX_PANE": "%1"},
                _edit_payload(target, session_id="$1", agent_id="a9", agent_type="explore"),
            )
    finally:
        control.clear_animating("@1")
    border_calls = [c.args[0] for c in run.call_args_list if "pane-border-style" in c.args[0]]
    assert border_calls == []  # skipped: registered, never tinted
    assert state.FollowerState.read("@1").writers == ("a9",)  # type: ignore[union-attr]


def test_register_writer_is_a_noop_when_the_identity_is_already_present(tmp_path: Path) -> None:
    _register_fake_follower("@1", "%2", shown_any=True)
    state.FollowerState.update("@1", writers=("$1",), writer_labels=("session:$1",))
    hooks._register_writer("@1", {"session_id": "$1"})
    result = state.FollowerState.read("@1")
    assert result.writers == ("$1",)  # type: ignore[union-attr]
    assert result.writer_labels == ("session:$1",)  # type: ignore[union-attr]


def test_apply_writer_cue_is_a_noop_when_the_identity_is_not_yet_registered(
    tmp_path: Path,
) -> None:
    _register_fake_follower("@1", "%2", shown_any=True)
    state.FollowerState.update("@1", writers=("$1", "a9"), writer_labels=("session:$1", "explore"))
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        hooks._apply_writer_cue("@1", "%2", {"session_id": "$unknown"})
    border_calls = [c.args[0] for c in run.call_args_list if "pane-border-style" in c.args[0]]
    assert border_calls == []


def test_apply_writer_cue_routes_nvim_backed_windows_to_the_nvim_status_surface(
    tmp_path: Path,
) -> None:
    # The nvim backend has no tmux pane border to tint, so this must go
    # through NvimStatusSurface (a floating window + virtual text), never
    # construct a TmuxStatusSurface around the socket path.
    state.FollowerState.set(
        "@1",
        "nvim",
        "/tmp/x.sock",
        open_files=(),
        shown_any=True,
        writers=("$1", "a9"),
        writer_labels=("session:$1", "code-reviewer"),
    )
    nvim = MagicMock()
    with (
        patch("pynvim.attach", return_value=nvim) as attach,
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
    ):
        hooks._apply_writer_cue(
            "@1", "/tmp/x.sock", {"agent_id": "a9", "agent_type": "code-reviewer"}
        )
    # Every connection targets the socket: the cue's own, plus the hit-enter
    # watchdog's on a dedicated follower (see backends/nvim_prompt.py).
    assert attach.call_args_list
    assert all(
        c.args == ("socket",) and c.kwargs == {"path": "/tmp/x.sock"} for c in attach.call_args_list
    )
    border_calls = [c.args[0] for c in run.call_args_list if "pane-border-style" in c.args[0]]
    assert border_calls == []


# --- standalone (no tmux) hook wiring -------------------------------------


def test_standalone_post_edit_hook_animates_via_nvim_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No TMUX_PANE: the session resolves to a standalone identity keyed on
    # TERM_SESSION_ID. An existing standalone nvim follower must still
    # animate through the ordinary RPC path — the animation itself is
    # backend-only (never tmux-only), and is driven with the session's
    # (standalone) window_id, not a tmux #{window_id}.
    target = tmp_path / "f.txt"
    target.write_text("hello\n")
    state.FollowerState.set(
        "term-x",
        "nvim",
        "/tmp/standalone.sock",
        open_files=(),
        shown_any=True,
    )
    follower = MagicMock()
    follower.show_fresh.return_value = AnimationResult("completed", 1)
    captured: dict[str, object] = {}

    def _fake_get_follower(
        backend: str, follower_target: str, *args: object, **kwargs: object
    ) -> MagicMock:
        captured["backend"] = backend
        captured["target"] = follower_target
        captured["window_id"] = kwargs.get("window_id")
        return follower

    monkeypatch.setattr(hooks, "get_follower", _fake_get_follower)
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}

    # FollowerState.get's own liveness probe uses the real nvim backend
    # (not the hooks.get_follower fake above) to check is_alive() — give it
    # a socket that answers.
    with (
        patch("pynvim.attach", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
    ):
        assert hooks.cmd_hook_post({"TERM_SESSION_ID": "x"}, payload) == 0

    assert captured == {"backend": "nvim", "target": "/tmp/standalone.sock", "window_id": "term-x"}
    follower.show_fresh.assert_called_once_with(str(target), "hello\n", in_new_tab=True)
    refreshed = state.FollowerState.read("term-x")
    assert refreshed is not None
    assert refreshed.current_file == str(target)


def test_maybe_auto_open_standalone_nvim_auto_launches_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "auto", "open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    cfg = config.load()
    standalone = Session(window_id="term-x", origin=None, in_tmux=False)

    with (
        patch("vim_ai_follower.hooks.launch_standalone_nvim", return_value="/s.sock") as launch,
        patch("pynvim.attach", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
    ):
        result = hooks._maybe_auto_open(standalone, "/tmp/f.txt", cfg)

    launch.assert_called_once_with(
        "term-x", wait_seconds=nvim_connect.AUTO_OPEN_SOCKET_WAIT_SECONDS
    )
    assert result is not None
    assert result.backend == "nvim"
    assert result.target == "/s.sock"
    assert result.adopted is False
    assert state.FollowerState.read("term-x") == result


def test_maybe_auto_open_in_tmux_nvim_window_always_uses_standalone_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # nvim_window=always opens a standalone window even inside tmux — the
    # auto-open path must honor it too (same as cmd_start), taking the
    # standalone launcher instead of the tmux split.
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "always", "open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    cfg = config.load()
    in_tmux = Session(window_id="@1", origin="%1", in_tmux=True)

    with (
        patch("vim_ai_follower.hooks.launch_standalone_nvim", return_value="/s.sock") as launch,
        patch("vim_ai_follower.hooks.resolve_nvim_target") as resolve,
        patch("vim_ai_follower.hooks.keybindings.register"),
        patch("pynvim.attach", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
    ):
        result = hooks._maybe_auto_open(in_tmux, "/tmp/f.txt", cfg)

    launch.assert_called_once_with("@1", wait_seconds=nvim_connect.AUTO_OPEN_SOCKET_WAIT_SECONDS)
    resolve.assert_not_called()  # always overrides the tmux split
    assert result is not None
    assert result.backend == "nvim" and result.target == "/s.sock" and result.adopted is False


def test_maybe_auto_open_standalone_nvim_window_never_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "never", "open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    cfg = config.load()
    standalone = Session(window_id="term-x", origin=None, in_tmux=False)

    with patch("vim_ai_follower.hooks.launch_standalone_nvim") as launch:
        result = hooks._maybe_auto_open(standalone, "/tmp/f.txt", cfg)

    launch.assert_not_called()
    assert result is None
    assert state.FollowerState.read("term-x") is None


def test_maybe_auto_open_standalone_tmux_backend_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tmux backend drives a tmux split — standalone has no tmux server
    # to attach to, and no session to fail gracefully into either: the hook
    # just silently does nothing (unlike cmd_start, which errors loudly).
    config_path = tmp_path / "config.json"
    config_path.write_text('{"open_policy": "always"}')  # backend defaults to tmux
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    cfg = config.load()
    standalone = Session(window_id="term-x", origin=None, in_tmux=False)

    with patch("vim_ai_follower.hooks.launch_standalone_nvim") as launch:
        result = hooks._maybe_auto_open(standalone, "/tmp/f.txt", cfg)

    launch.assert_not_called()
    assert result is None
    assert state.FollowerState.read("term-x") is None


def test_maybe_auto_open_standalone_launcher_failure_logs_and_noops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A launcher non-zero exit (e.g. osascript denied) must degrade the hook to
    # a no-op (log + None), never raise a traceback into the tool run.
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "auto", "open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    cfg = config.load()
    standalone = Session(window_id="term-x", origin=None, in_tmux=False)

    with patch(
        "vim_ai_follower.hooks.launch_standalone_nvim",
        side_effect=subprocess.CalledProcessError(1, ["osascript"]),
    ):
        result = hooks._maybe_auto_open(standalone, "/tmp/f.txt", cfg)

    assert result is None
    assert state.FollowerState.read("term-x") is None


def test_maybe_auto_open_standalone_nvim_that_never_listens_logs_and_noops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The auto-open twin of `start`: a launcher that exits 0 but starts no nvim
    # must not persist a follower pointing at a socket nothing listens on.
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "auto", "open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    cfg = config.load()
    standalone = Session(window_id="term-x", origin=None, in_tmux=False)
    never = NvimNeverListened("/c/nvim-term-x.sock", "nvim-qt", 10.0)

    with (
        patch("vim_ai_follower.hooks.launch_standalone_nvim", side_effect=never),
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
    ):
        result = hooks._maybe_auto_open(standalone, "/tmp/f.txt", cfg)

    assert result is None
    assert state.FollowerState.read("term-x") is None
    assert f"standalone nvim launch failed for term-x: {never}" in caplog.text


def _standalone_auto_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> config.Config:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "auto", "open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    return config.load()


def test_a_failed_auto_open_backs_off_instead_of_stalling_every_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A launcher that starts nothing made EVERY Edit/Write/Read hook wait out
    # the full socket deadline and relaunch. The auto-open waits briefly,
    # and after a failure it stays off for this window for a while (logged
    # once); `claude-follow start` is the explicit retry.
    cfg = _standalone_auto_open(tmp_path, monkeypatch)
    standalone = Session(window_id="term-x", origin=None, in_tmux=False)
    never = NvimNeverListened("/c/nvim-term-x.sock", "nvim-qt", 3.0)

    with (
        patch("vim_ai_follower.hooks.launch_standalone_nvim", side_effect=never) as launch,
        caplog.at_level(logging.INFO, logger="vim_ai_follower"),
    ):
        assert hooks._maybe_auto_open(standalone, "/tmp/f.txt", cfg) is None
        assert hooks._maybe_auto_open(standalone, "/tmp/g.txt", cfg) is None
        assert hooks._maybe_auto_open(standalone, "/tmp/h.txt", cfg) is None

    assert launch.call_args_list == [
        mock_call("term-x", wait_seconds=nvim_connect.AUTO_OPEN_SOCKET_WAIT_SECONDS)
    ]
    assert nvim_connect.AUTO_OPEN_SOCKET_WAIT_SECONDS <= 3.0
    assert caplog.text.count("standalone nvim launch failed for term-x") == 1
    assert "not auto-opening term-x again for 5 minutes" in caplog.text


def test_the_auto_open_backoff_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _standalone_auto_open(tmp_path, monkeypatch)
    standalone = Session(window_id="term-x", origin=None, in_tmux=False)
    never = NvimNeverListened("/c/nvim-term-x.sock", "nvim-qt", 3.0)
    with patch("vim_ai_follower.hooks.launch_standalone_nvim", side_effect=never) as launch:
        hooks._maybe_auto_open(standalone, "/tmp/f.txt", cfg)
        marker = nvim_connect.launch_backoff_path("term-x")
        stale = time.time() - nvim_connect.LAUNCH_BACKOFF_SECONDS - 1
        os.utime(marker, (stale, stale))
        hooks._maybe_auto_open(standalone, "/tmp/g.txt", cfg)
    assert launch.call_count == 2


def test_an_nvim_that_binds_after_the_auto_open_gave_up_is_attached_despite_the_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A launcher that returns at once and binds nvim a few seconds later (a
    # Terminal `do script` behind a slow shell rc): hook 1 gives up and backs
    # off; the nvim then listens. Hook 2 used to return in 0 s on the backoff
    # — a window on screen and nothing animating for 5 minutes, then a second
    # launch whose nvim died on "address already in use" (slow_launch.py).
    cfg = _standalone_auto_open(tmp_path, monkeypatch)
    standalone = Session(window_id="term-x", origin=None, in_tmux=False)
    directory = Path(tempfile.mkdtemp(prefix="vafs", dir="/tmp"))  # short: AF_UNIX
    sock = directory / "nvim-term-x.sock"
    late_nvim = ExitStack()
    try:
        with (
            patch(
                "vim_ai_follower.backends.nvim_connect.state.nvim_socket_path", return_value=sock
            ),
            patch(
                "vim_ai_follower.hooks.launch_standalone_nvim",
                side_effect=NvimNeverListened(str(sock), "osascript", 3.0),
            ) as launch,
        ):
            assert hooks._maybe_auto_open(standalone, "/tmp/f.txt", cfg) is None
            assert nvim_connect.launch_backoff_active("term-x")
            late_nvim.enter_context(fake_nvim(sock))  # the slow nvim finally answers
            # The launcher step of hook 2's auto-open (the rest of it would
            # talk RPC to this stand-in; the e2e drives a real nvim).
            assert hooks._launch_standalone_or_log("term-x") == str(sock)
        assert launch.call_count == 1  # attached, not relaunched
        assert not nvim_connect.launch_backoff_active("term-x")
    finally:
        late_nvim.close()
        shutil.rmtree(directory, ignore_errors=True)


def test_an_auto_open_whose_socket_path_is_blocked_logs_and_noops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _standalone_auto_open(tmp_path, monkeypatch)
    standalone = Session(window_id="term-x", origin=None, in_tmux=False)
    blocked = nvim_connect.SocketPathBlocked("/c/nvim-term-x.sock", PermissionError(1, "denied"))
    with patch("vim_ai_follower.hooks.launch_standalone_nvim", side_effect=blocked):
        assert hooks._maybe_auto_open(standalone, "/tmp/f.txt", cfg) is None
    assert state.FollowerState.read("term-x") is None


def test_maybe_auto_open_in_tmux_always_launcher_failure_logs_and_noops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same guard on the nvim_window=always override taken while inside tmux.
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "always", "open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    cfg = config.load()
    in_tmux = Session(window_id="@1", origin="%1", in_tmux=True)

    with (
        patch("vim_ai_follower.hooks.keybindings.register"),
        patch(
            "vim_ai_follower.hooks.launch_standalone_nvim",
            side_effect=subprocess.CalledProcessError(1, ["nvim-qt"]),
        ),
    ):
        result = hooks._maybe_auto_open(in_tmux, "/tmp/f.txt", cfg)

    assert result is None


def test_hook_pre_dead_tmux_pane_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # TMUX_PANE is set but tmux can't resolve a window for it (a dead pane)
    # — resolve_session returns None, treated exactly like the old
    # TmuxWindow.from_env(env) is None case.
    monkeypatch.setattr(hooks, "resolve_session", lambda env: None)
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/f.txt"}}
    assert hooks.cmd_hook_pre({"TMUX_PANE": "%1"}, payload) == 0


def test_hook_post_dead_tmux_pane_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "resolve_session", lambda env: None)
    edit_payload: dict[str, object] = {
        "tool_name": "Edit",
        "tool_input": {"file_path": "/tmp/f.txt"},
    }
    assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, edit_payload) == 0
    read_payload: dict[str, object] = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/f.txt"},
    }
    assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, read_payload) == 0


def test_completed_animation_rerenders_the_writer_cue_over_any_stale_transient(
    tmp_path: Path,
) -> None:
    # A pause/resume runs in ANOTHER process (cmd_pause), and that process's
    # surface save/restore dies with it — so a completed animation must
    # re-assert the persistent cue from FollowerState, wiping any stale
    # "Paused"/"Writing..." body it may have left behind. Concretely: the
    # border tint fires once before animating and AGAIN at completion.
    target = tmp_path / "a.py"
    target.write_text("x\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _edit_payload(target, session_id="$1"))
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        hooks.cmd_hook_post(
            {"TMUX_PANE": "%1"},
            _edit_payload(target, session_id="$1", agent_id="a9", agent_type="code-reviewer"),
        )
    tint = [
        "tmux",
        "set-option",
        "-p",
        "-t",
        "%2",
        "pane-border-style",
        f"fg={writer_cue.PALETTE[1]}",
    ]
    calls = [c.args[0] for c in run.call_args_list]
    assert calls.count(tint) == 2  # once entering the edit, once at completion


def test_completed_single_writer_animation_clears_any_stale_transient_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With fewer than two writers there is no persistent cue to re-render,
    # but a cross-process pause may still have left a transient body (the
    # nvim float saying "Paused"/"Writing...") — completion clears it.
    target = tmp_path / "a.py"
    target.write_text("x\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    surface = MagicMock()
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: surface)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _edit_payload(target, session_id="$1"))
    surface.clear.assert_called_once()
    surface.set_writer.assert_not_called()  # one writer: never a persistent cue


def test_animation_shows_writing_state_before_typing_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The float/border body must say "Writing..." while an ordinary
    # animation is in flight. Before this fix, only the pause-resume and
    # des-interrupt-replay paths ever called set_state("Writing...") — a
    # ordinary first-time edit with no pause/interrupt left the body blank
    # for the whole animation.
    target = tmp_path / "a.py"
    target.write_text("x\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    surface = MagicMock()
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: surface)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _edit_payload(target, session_id="$1"))
    surface.set_state.assert_called_once_with("Writing...")


def test_refresh_writer_cue_is_a_noop_when_state_vanished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A concurrent `stop` can delete the window's state between the animation
    # completing and the cue refresh (the decoupled-lifecycle race this
    # project already defends elsewhere): nothing to render, touch no surface.
    surface = MagicMock()
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: surface)
    hooks._refresh_writer_cue("@gone", "%2", {"session_id": "$1"})
    surface.clear.assert_not_called()
    surface.set_writer.assert_not_called()


def test_hook_post_read_builds_the_follower_with_its_window_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without the window id an NvimFollower cannot read its FollowerState, so
    # an ADOPTED nvim looked dedicated on Read navigation: the hit-enter
    # watchdog would answer the user's own prompts (backends/nvim_prompt.py),
    # and ensure_showing would lock the user's buffer nomodifiable.
    target = tmp_path / "f.py"
    target.write_text("a\n")
    state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=True)
    current = state.FollowerState.read("@1")
    captured: dict[str, object] = {}

    def _fake_get_follower(backend: str, follower_target: str, **kwargs: object) -> MagicMock:
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(hooks, "get_follower", _fake_get_follower)
    monkeypatch.setattr(hooks, "_live_follower_healing_keys", lambda session: current)
    payload: dict[str, object] = {"tool_name": "Read", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0
    assert captured.get("window_id") == "@1"


def _hook_log_lines() -> list[str]:
    return hooks.LOG_PATH.read_text().splitlines() if hooks.LOG_PATH.exists() else []


def test_an_edit_the_follower_cannot_reach_is_skipped_in_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NavigationFailed means the backend typed nothing after the failed
    navigation. The hook skips the edit: rc 0, one log line and no
    traceback, the "Writing..." cue taken down, the slot released, and the
    file marked stale so its next touch does not type a diff onto a buffer
    this edit never reached."""
    target = tmp_path / "a.py"
    target.write_text("x\n")
    file_path = os.path.realpath(str(target))
    snapshot.save("@1", file_path, "w\n")
    _register_fake_follower("@1", "%2", open_files=(file_path,), shown_any=True)
    surface = MagicMock()
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: surface)
    failure = NavigationFailed(f"the follower's Vim did not land on {file_path} (no answer)")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch.object(TmuxVimFollower, "probe_buffer", return_value="holds"),
        patch.object(TmuxVimFollower, "apply_edit", side_effect=failure),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _edit_payload(target, session_id="$1")) == 0

    lines = _hook_log_lines()
    skipped = [line for line in lines if "skipped the animation" in line]
    assert len(skipped) == 1, lines
    assert "(no answer)" in skipped[0]
    assert not any("Traceback" in line or "crashed" in line for line in lines), lines
    surface.set_state.assert_called_once_with("Writing...")
    surface.clear.assert_called_once()
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert file_path in refreshed.stale_files
    assert control.animating_state("@1") is None


def test_a_read_the_follower_cannot_reach_moves_no_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.py"
    target.write_text("x\ny\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    payload: dict[str, object] = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target), "offset": 2},
    }
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch.object(
            TmuxVimFollower, "ensure_showing", side_effect=NavigationFailed("did not land")
        ),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    assert ":exe 2|redraw|echo ''" not in _literal_sends(run)
    lines = _hook_log_lines()
    assert [line for line in lines if "skipped the Read" in line and "did not land" in line]
    assert not any("Traceback" in line for line in lines), lines
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert os.path.realpath(str(target)) not in refreshed.open_files


def test_an_unreachable_edit_after_a_concurrent_stop_touches_no_surface(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # `stop` may delete the window's state while the hook runs: the skip still
    # logs, and there is no surface left to refresh.
    surface = MagicMock()
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: surface)
    hooks._skip_unreachable_edit("@gone", "/tmp/a.py", {}, NavigationFailed("x"))
    surface.clear.assert_not_called()
    assert "skipped the animation of /tmp/a.py: x" in caplog.text


_SILENT = ("silent", False, None)
# Blocking, but not at a prompt the follower answers: keys pending (a `q`
# waiting for its register, half of a mapping). Nothing to sweep there.
_ADOPTED_BLOCKED = ("answers", True, "n")
_ADOPTED_AT_A_PROMPT = ("answers", True, "r")
# Not blocking, but the user's answer (a confirm: `confirm()`, `:s///c`) or
# Enter (the command line: `input()`, a half-typed `:` command) lands on
# whatever buffer is current then (task 22 review I1).
_ADOPTED_AT_A_CONFIRM = ("answers", False, "r?")
_ADOPTED_ON_THE_CMDLINE = ("answers", False, "c")


@pytest.mark.parametrize(
    ("probe_answer", "adopted", "why"),
    [
        (_SILENT, False, "did not answer within 1 s"),
        (_ADOPTED_BLOCKED, True, "is waiting on its user (in the middle of a key sequence)"),
        (_ADOPTED_AT_A_CONFIRM, True, "is waiting on its user (a confirm prompt)"),
        (_ADOPTED_ON_THE_CMDLINE, True, "is waiting on its user (on the command line)"),
        (("answers", False, "cv"), True, "is waiting on its user (on the command line)"),
        # A `d` waiting for its motion: the user's second `d` deleted Claude's
        # line (T22 re-review I2). Not blocking.
        (("answers", False, "no"), True, "is waiting on its user (an operator pending)"),
        (("answers", False, "nov"), True, "is waiting on its user (an operator pending)"),
        (("answers", False, "noV"), True, "is waiting on its user (an operator pending)"),
        (("answers", False, "no\x16"), True, "is waiting on its user (an operator pending)"),
        # The user is typing: a navigation keeps the mode, so their next keys
        # land in Claude's buffer (measured, nvim 0.12.6; Alberto 2026-10-17).
        (("answers", False, "i"), True, "is waiting on its user (in insert mode)"),
        (("answers", False, "ic"), True, "is waiting on its user (in insert mode)"),
        (("answers", False, "niI"), True, "is waiting on its user (in insert mode)"),
        (("answers", False, "R"), True, "is waiting on its user (in replace mode)"),
        (("answers", False, "Rv"), True, "is waiting on its user (in replace mode)"),
        (("answers", False, "t"), True, "is waiting on its user (in terminal mode)"),
        (("answers", False, "ntT"), True, "is waiting on its user (in terminal mode)"),
        (("answers", False, "niR"), True, "is waiting on its user (in replace mode)"),
        (("answers", False, "niV"), True, "is waiting on its user (in replace mode)"),
        # Visual and select: a navigation leaves the user in Normal mode in
        # Claude's buffer, and their next `x`/`d` acted there (review I1 of
        # T24a, measured on nvim 0.12.6; Alberto 2026-10-17).
        (("answers", False, "v"), True, "is waiting on its user (in visual mode)"),
        (("answers", False, "V"), True, "is waiting on its user (in visual mode)"),
        (("answers", False, "\x16"), True, "is waiting on its user (in visual mode)"),
        (("answers", False, "vs"), True, "is waiting on its user (in select mode)"),
        (("answers", False, "Vs"), True, "is waiting on its user (in select mode)"),
        (("answers", False, "\x16s"), True, "is waiting on its user (in select mode)"),
        (("answers", False, "s"), True, "is waiting on its user (in select mode)"),
        (("answers", False, "S"), True, "is waiting on its user (in select mode)"),
        (("answers", False, "\x13"), True, "is waiting on its user (in select mode)"),
    ],
    ids=[
        "silent",
        "adopted-blocked",
        "adopted-confirm",
        "adopted-cmdline",
        "adopted-cmdline-ex",
        "adopted-operator",
        "adopted-operator-v",
        "adopted-operator-V",
        "adopted-operator-ctrl-v",
        "adopted-insert",
        "adopted-insert-completion",
        "adopted-insert-ctrl-o",
        "adopted-replace",
        "adopted-virtual-replace",
        "adopted-terminal",
        "adopted-terminal-ctrl-o",
        "adopted-replace-ctrl-o",
        "adopted-virtual-replace-ctrl-o",
        "adopted-visual",
        "adopted-visual-line",
        "adopted-visual-block",
        "adopted-visual-ctrl-o-from-select",
        "adopted-visual-line-ctrl-o-from-select",
        "adopted-visual-block-ctrl-o-from-select",
        "adopted-select",
        "adopted-select-line",
        "adopted-select-block",
    ],
)
@pytest.mark.parametrize("tool_name", ["Edit", "Read"])
def test_a_hook_skips_a_busy_nvim_follower_and_keeps_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tool_name: str,
    probe_answer: tuple[str, bool],
    adopted: bool,
    why: str,
) -> None:
    # The probe found it accepting and silent (`:!make`, `system()`), or
    # found the user's own nvim answering `blocking: true` (a hit-enter
    # prompt already up when the hook starts: no watchdog there): alive, so
    # no reopen and no "no follower"; but every request would wait out the
    # deadline (cue, base probe, cue refresh: 30 s and a traceback), so the
    # hook sends nothing — an Edit marks the file stale.
    target = tmp_path / "f.txt"
    target.write_text("hello\n")
    state.FollowerState.set(
        "term-x",
        "nvim",
        "/tmp/busy.sock",
        open_files=(str(target),),
        shown_any=True,
        on_failure="reopen",
        origin="%9",
        adopted=adopted,
    )
    get_follower = MagicMock()
    monkeypatch.setattr(hooks, "get_follower", get_follower)
    monkeypatch.setattr(hooks, "status_surface_for", get_follower)
    payload: dict[str, object] = {"tool_name": tool_name, "tool_input": {"file_path": str(target)}}
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        patch("vim_ai_follower.backends.nvim_connect._probe", return_value=probe_answer),
        patch("vim_ai_follower.hooks.resolve_nvim_target") as relaunch,
    ):
        assert hooks.cmd_hook_post({"TERM_SESSION_ID": "x"}, payload) == 0
    get_follower.assert_not_called()
    relaunch.assert_not_called()
    after = state.FollowerState.read("term-x")
    assert after is not None and after.target == "/tmp/busy.sock"
    assert after.stale_files == ((str(target),) if tool_name == "Edit" else ())
    skipped = [line for line in caplog.text.splitlines() if why in line]
    assert len(skipped) == 1, caplog.text
    assert "/tmp/busy.sock" in skipped[0] and str(target) in skipped[0]
    assert "Traceback" not in caplog.text


def test_a_dedicated_nvim_at_a_prompt_is_not_skipped() -> None:
    # Its prompt watchdog dismisses the follower's own prompts, so a blocking
    # answer from a DEDICATED nvim is no reason to skip.
    current = state.FollowerState(
        backend="nvim",
        target="/tmp/own.sock",
        current_file=None,
        origin="%9",
        on_failure="silent",
        speed="normal",
        adopted=False,
    )
    for answer in (
        _ADOPTED_BLOCKED,
        _ADOPTED_AT_A_CONFIRM,
        _ADOPTED_ON_THE_CMDLINE,
        ("answers", False, "i"),
        ("answers", False, "t"),
    ):
        with patch("vim_ai_follower.backends.nvim_connect._probe", return_value=answer):
            nvim_connect.probe_nvim("/tmp/own.sock")
        assert hooks._busy_nvim(current) is None
        assert hooks._busy_nvim(dataclasses.replace(current, adopted=True)) is not None


# Insert, replace, terminal, visual and select mode are skipped since
# Alberto's 2026-10-17 answers; Normal and Terminal-Normal (`nt`) are not.
@pytest.mark.parametrize("mode", ["n", "nt", None])
def test_an_adopted_nvim_in_an_ordinary_mode_is_not_skipped(mode: str | None) -> None:
    current = state.FollowerState(
        backend="nvim",
        target="/tmp/user.sock",
        current_file=None,
        origin="%9",
        on_failure="silent",
        speed="normal",
        adopted=True,
    )
    with patch(
        "vim_ai_follower.backends.nvim_connect._probe", return_value=("answers", False, mode)
    ):
        nvim_connect.probe_nvim("/tmp/user.sock")
    with patch(
        "vim_ai_follower.backends.nvim_connect.in_command_line_window", return_value=False
    ) as cmdwin:
        assert hooks.adopted_user_pending("/tmp/user.sock") is None
        assert hooks._busy_nvim(current) is None
    cmdwin.assert_called_with("/tmp/user.sock")


@pytest.mark.parametrize("answer", [True, False, None])
def test_an_adopted_nvim_in_its_command_line_window_is_skipped(
    answer: bool | None, caplog: pytest.LogCaptureFixture
) -> None:
    # `q:` reads "n", not blocking, and every window change in it raises E11
    # (show_fresh crashed the hook, T22 re-review m2). No answer (None) is
    # not proof of the cmdwin: the follower acts as before.
    current = state.FollowerState(
        backend="nvim",
        target="/tmp/user.sock",
        current_file=None,
        origin="%9",
        on_failure="silent",
        speed="normal",
        adopted=True,
    )
    with patch(
        "vim_ai_follower.backends.nvim_connect._probe", return_value=("answers", False, "n")
    ):
        nvim_connect.probe_nvim("/tmp/user.sock")
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        patch("vim_ai_follower.backends.nvim_connect.in_command_line_window", return_value=answer),
    ):
        busy = hooks._busy_nvim(current)
    if answer:
        assert busy == "nvim at /tmp/user.sock is waiting on its user (in the command-line window)"
    else:
        assert busy is None


@pytest.mark.parametrize(
    "probe_answer", [("answers", True, "r"), ("answers", True, "n"), ("silent", None, None)]
)
def test_the_command_line_window_is_asked_only_of_an_nvim_answering_not_blocking(
    probe_answer: tuple[str, bool | None, str | None],
) -> None:
    # An ordinary request is held at a blocking prompt, and a silent nvim
    # answers nothing: asking would cost its whole timeout.
    with patch("vim_ai_follower.backends.nvim_connect._probe", return_value=probe_answer):
        nvim_connect.probe_nvim("/tmp/user.sock")
    with patch("vim_ai_follower.backends.nvim_connect.in_command_line_window") as cmdwin:
        hooks.adopted_user_pending("/tmp/user.sock")
    cmdwin.assert_not_called()


@pytest.mark.parametrize("mode", ["r", "rm"])
def test_an_adopted_nvim_at_a_hit_enter_or_more_prompt_is_swept_not_skipped(
    mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    # Decision (a): the Vim follower's Escape pair dismisses a prompt the
    # user has up when it is about to act; so does this sweep, before the
    # cue is raised (a non-acting request that would wait on the prompt).
    current = state.FollowerState(
        backend="nvim",
        target="/tmp/user.sock",
        current_file=None,
        origin="%9",
        on_failure="silent",
        speed="normal",
        adopted=True,
    )
    connection = MagicMock()
    prompt = {"mode": mode, "blocking": True}
    modes = iter([prompt, prompt])
    connection.api.get_mode.side_effect = lambda: next(modes, {"mode": "n", "blocking": False})
    connection.api.exec2.return_value = {"output": "plugin says\n"}
    # The second connection is the watchdog's, over the `:messages` read.
    watchdog = MagicMock()
    watchdog.api.get_mode.return_value = {"mode": "n", "blocking": False}
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        patch("vim_ai_follower.backends.nvim_connect._probe", return_value=("answers", True, mode)),
        patch(
            "vim_ai_follower.backends.nvim_connect.bounded_attach",
            side_effect=[connection, watchdog],
        ) as attach,
    ):
        nvim_connect.probe_nvim("/tmp/user.sock")
        assert hooks._busy_nvim(current) is None
    assert attach.call_args_list == [mock_call("/tmp/user.sock")] * 2
    watchdog.api.input.assert_not_called()
    connection.api.input.assert_called_once_with("<CR>" if mode == "r" else "<Esc>")
    assert "left before the hook (sweep)" in caplog.text
    assert "waiting on its user" not in caplog.text


def test_an_adopted_prompt_that_cannot_be_reached_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    current = state.FollowerState(
        backend="nvim",
        target="/tmp/user.sock",
        current_file=None,
        origin="%9",
        on_failure="silent",
        speed="normal",
        adopted=True,
    )
    with (
        patch("vim_ai_follower.backends.nvim_connect._probe", return_value=("answers", True, "r")),
        patch(
            "vim_ai_follower.backends.nvim_connect.bounded_attach",
            side_effect=nvim_connect.RpcTimeout("nvim did not answer within 3 s"),
        ),
    ):
        nvim_connect.probe_nvim("/tmp/user.sock")
        busy = hooks._busy_nvim(current)
    assert busy is not None
    assert "could not be answered" in busy and "did not answer within 3 s" in busy


def test_a_base_probe_that_times_out_logs_one_line(caplog: pytest.LogCaptureFixture) -> None:
    # A prompt raised after the liveness probe: the base probe's timeout is
    # one line, not a traceback.
    follower = MagicMock()
    follower.probe_buffer.side_effect = nvim_connect.RpcTimeout("nvim did not answer within 10 s")
    with caplog.at_level(logging.WARNING, logger="vim_ai_follower"):
        assert hooks._probe_base(follower, "/tmp/a.py", "x\n") == "unknown"
    assert caplog.text.count("\n") == 1, caplog.text
    assert "/tmp/a.py" in caplog.text and "did not answer within 10 s" in caplog.text


@pytest.mark.parametrize("tool_name", ["Edit", "Read"])
def test_an_rpc_timeout_outside_the_typing_skips_in_one_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tool_name: str,
) -> None:
    # An nvim that stops answering at the connect or a navigation (the typing
    # itself waits): no "crashed" traceback; an Edit marks the file stale and
    # refreshes the cue, a Read changes nothing.
    target = tmp_path / "f.txt"
    target.write_text("hello\n")
    state.FollowerState.set("term-x", "nvim", "/tmp/s.sock", open_files=(str(target),))
    timeout = nvim_connect.RpcTimeout("nvim did not answer within 10 s")
    monkeypatch.setattr(hooks, "_animate_edit", MagicMock(side_effect=timeout))
    monkeypatch.setattr(hooks, "_navigate_for_read", MagicMock(side_effect=timeout))
    refresh = MagicMock()
    monkeypatch.setattr(hooks, "_refresh_writer_cue", refresh)
    payload: dict[str, object] = {"tool_name": tool_name, "tool_input": {"file_path": str(target)}}
    with caplog.at_level(logging.WARNING, logger="vim_ai_follower"):
        assert (
            hooks.run_hook("post", {"TERM_SESSION_ID": "x"}, io.StringIO(json.dumps(payload))) == 0
        )
    assert "crashed" not in caplog.text
    skipped = [line for line in caplog.text.splitlines() if "nvim did not answer within" in line]
    assert len(skipped) == 1 and str(target) in skipped[0], caplog.text
    after = state.FollowerState.read("term-x")
    assert after is not None
    assert after.stale_files == ((str(target),) if tool_name == "Edit" else ())
    assert refresh.called is (tool_name == "Edit")


# --- a stopped window stays stopped: auto-open honours the tombstone (T17) --


def _auto_open_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    target = tmp_path / "f.txt"
    target.write_text("hello\n")
    return {"tool_name": "Write", "tool_input": {"file_path": str(target)}}


def test_auto_open_does_not_reopen_a_stopped_window_and_says_so_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _auto_open_payload(tmp_path, monkeypatch)
    state.mark_stopped("@1", 4242, in_tmux=True)
    with (
        patch(
            "vim_ai_follower.tmux.subprocess.run",
            side_effect=_mock_tmux_run(other_panes=("%1",)),
        ) as run,
        patch("vim_ai_follower.binding.tmux_server_pid", return_value=4242),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0
    assert not any(c.args[0][:2] == ["tmux", "split-window"] for c in run.call_args_list)
    assert state.FollowerState.read("@1") is None
    log = hooks.LOG_PATH.read_text()
    assert log.count("claude-follow stop") == 1, log


def test_auto_open_ignores_a_tombstone_from_another_tmux_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _auto_open_payload(tmp_path, monkeypatch)
    state.mark_stopped("@1", 4242, in_tmux=True)
    with (
        patch(
            "vim_ai_follower.tmux.subprocess.run",
            side_effect=_mock_tmux_run(other_panes=("%1",)),
        ) as run,
        patch("vim_ai_follower.binding.tmux_server_pid", return_value=70001),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0
    assert any(c.args[0][:2] == ["tmux", "split-window"] for c in run.call_args_list)
    assert state.FollowerState.read("@1") is not None


def test_auto_open_does_not_reopen_a_stopped_standalone_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "auto", "open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    state.mark_stopped("term-x", None, in_tmux=False)
    standalone = Session(window_id="term-x", origin=None, in_tmux=False)
    with patch("vim_ai_follower.hooks.launch_standalone_nvim") as launch:
        assert hooks._maybe_auto_open(standalone, "/tmp/f.txt", config.load()) is None
    launch.assert_not_called()


# --- final review of T17: a launch a stop raced, a state from another server -


def _kills(run: MagicMock) -> list[list[str]]:
    return [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "kill-pane"]]


def test_auto_open_closes_the_pane_it_opened_when_a_stop_lands_before_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Minor 3: the stop lands after _stopped_window said "not stopped" and
    # before the write, which the tombstone then refuses.
    payload = _auto_open_payload(tmp_path, monkeypatch)
    state.mark_stopped("@1", 4242, in_tmux=True)
    with (
        patch(
            "vim_ai_follower.tmux.subprocess.run",
            side_effect=_mock_tmux_run(other_panes=("%1",)),
        ) as run,
        patch("vim_ai_follower.hooks._stopped_window", return_value=False),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0
    assert any(c.args[0][:2] == ["tmux", "split-window"] for c in run.call_args_list)
    assert _kills(run) == [["tmux", "kill-pane", "-t", "%2"]]
    assert state.FollowerState.read("@1") is None
    assert "was stopped while its follower was opening" in hooks.LOG_PATH.read_text()


def test_auto_open_quits_the_nvim_it_launched_when_a_stop_lands_before_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    state.mark_stopped("@1", 4242, in_tmux=True)
    session = Session(window_id="@1", origin="%1", in_tmux=True)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.hooks._stopped_window", return_value=False),
        patch("vim_ai_follower.hooks.resolve_nvim_target", return_value=("/tmp/n.sock", True)),
        patch("vim_ai_follower.backends.nvim.NvimFollower.stop") as nvim_stop,
    ):
        assert hooks._maybe_auto_open(session, "/tmp/f.txt", config.load()) is None
    nvim_stop.assert_called_once_with(dedicated=True)


def test_auto_open_leaves_an_adopted_nvim_alone_when_a_stop_lands_before_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "open_policy": "always", "adopt_existing": true}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    state.mark_stopped("@1", 4242, in_tmux=True)
    session = Session(window_id="@1", origin="%1", in_tmux=True)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.hooks._stopped_window", return_value=False),
        patch("vim_ai_follower.hooks.resolve_nvim_target", return_value=("/tmp/u.sock", False)),
        patch("vim_ai_follower.backends.nvim.NvimFollower.stop") as nvim_stop,
    ):
        assert hooks._maybe_auto_open(session, "/tmp/f.txt", config.load()) is None
    nvim_stop.assert_not_called()


def test_a_launch_that_cannot_be_closed_for_a_stopped_window_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state.mark_stopped("@1", 4242, in_tmux=True)
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch(
            "vim_ai_follower.backends.tmux_vim.TmuxVimFollower.stop",
            side_effect=subprocess.CalledProcessError(1, ["tmux", "kill-pane"]),
        ),
    ):
        assert hooks._launched_follower("@1", "tmux", "%2", recorded=False) is None
    assert "could not close the follower %2 opened" in caplog.text


def test_reopen_closes_the_pane_it_opened_when_a_stop_lands_before_the_write() -> None:
    _register_fake_follower("@1", "%5")
    state.FollowerState.update("@1", on_failure="reopen", origin="%1")
    state.mark_stopped("@1", 4242, in_tmux=True)
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=_mock_tmux_run(pane_id="%2", pane_exists=False),
    ) as run:
        # The dead %5 is not listed, so get() fails and the reopen splits.
        assert hooks._get_active_follower("@1") is None
    assert any(c.args[0][:2] == ["tmux", "split-window"] for c in run.call_args_list)
    assert _kills(run) == [["tmux", "kill-pane", "-t", "%2"]]


def test_reopen_never_splits_from_a_state_left_by_another_tmux_server() -> None:
    # After a tmux restart the recorded origin is a pane of the new server
    # that is not this window's.
    _register_fake_follower("@1", "%5", server_pid=1111)
    state.FollowerState.update("@1", on_failure="reopen", origin="%1")
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=_mock_tmux_run(pane_id="%2", pane_exists=False),
    ) as run:
        assert hooks._get_active_follower("@1") is None
    assert not any(c.args[0][:2] == ["tmux", "split-window"] for c in run.call_args_list)


def test_a_window_never_stopped_costs_auto_open_no_tmux_spawn() -> None:
    # Minor 7: one stat, like set()'s check.
    session = Session(window_id="@1", origin="%1", in_tmux=True)
    with patch("vim_ai_follower.binding.tmux_server_pid") as server_pid:
        assert hooks._stopped_window(session) is False
    server_pid.assert_not_called()


# --- a tmux split nvim that never answers (T19, answer B) -----------------


def _split_never_answered(pane_open: bool = True) -> nvim_connect.SplitNvimNeverAnswered:
    return nvim_connect.SplitNvimNeverAnswered("/c/nvim-@1.sock", 3.0, "%7", pane_open=pane_open)


def _tmux_nvim_auto_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> config.Config:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "open_policy": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    return config.load()


def test_a_split_nvim_that_never_answers_is_not_recorded_and_backs_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _tmux_nvim_auto_open(tmp_path, monkeypatch)
    in_tmux = Session(window_id="@1", origin="%1", in_tmux=True)
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.hooks._stopped_window", return_value=False),
        patch(
            "vim_ai_follower.hooks.resolve_nvim_target", side_effect=_split_never_answered()
        ) as resolve,
    ):
        assert hooks._maybe_auto_open(in_tmux, "/tmp/f.txt", cfg) is None
        # Within the backoff the next hook does not split another pane next
        # to the one still showing nvim's error.
        assert hooks._maybe_auto_open(in_tmux, "/tmp/g.txt", cfg) is None
    resolve.assert_called_once_with("%1", "@1", adopt=False)
    assert state.FollowerState.read("@1") is None
    assert nvim_connect.launch_backoff_active("@1")
    assert not [c for c in run.call_args_list if c.args[0][:2] == ["tmux", "kill-pane"]]
    message = (
        "nvim did not answer on /c/nvim-@1.sock within 3 s; "
        "its pane is left open so you can read why"
    )
    assert caplog.text.count(message) == 1
    assert "not auto-opening @1 again for 5 minutes" in caplog.text
    assert capsys.readouterr().err == f"claude-follow: {message}\n"


def test_the_split_backoff_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _tmux_nvim_auto_open(tmp_path, monkeypatch)
    in_tmux = Session(window_id="@1", origin="%1", in_tmux=True)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.hooks._stopped_window", return_value=False),
        patch(
            "vim_ai_follower.hooks.resolve_nvim_target", side_effect=_split_never_answered()
        ) as resolve,
    ):
        hooks._maybe_auto_open(in_tmux, "/tmp/f.txt", cfg)
        marker = nvim_connect.launch_backoff_path("@1")
        stale = time.time() - nvim_connect.LAUNCH_BACKOFF_SECONDS - 1
        os.utime(marker, (stale, stale))
        hooks._maybe_auto_open(in_tmux, "/tmp/g.txt", cfg)
    assert resolve.call_count == 2


def test_the_split_backoff_never_keeps_a_window_from_adopting_the_users_nvim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The backoff is about SPLITTING: an nvim the user runs in the window is
    # adopted as before, since adopting opens nothing.
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "open_policy": "always", "adopt_existing": true}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    nvim_connect.record_launch_failure("@1")
    in_tmux = Session(window_id="@1", origin="%1", in_tmux=True)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.hooks._stopped_window", return_value=False),
        patch("vim_ai_follower.hooks.nvim_connect.discover_adopt_socket", return_value="/u.sock"),
        patch(
            "vim_ai_follower.hooks.resolve_nvim_target", return_value=("/u.sock", False)
        ) as resolve,
    ):
        hooks._maybe_auto_open(in_tmux, "/tmp/f.txt", config.load())
    resolve.assert_called_once_with("%1", "@1", adopt=True)
    recorded = state.FollowerState.read("@1")
    assert recorded is not None
    assert recorded.adopted is True


def test_a_split_that_answers_late_is_attached_despite_the_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _tmux_nvim_auto_open(tmp_path, monkeypatch)
    nvim_connect.record_launch_failure("@1")
    in_tmux = Session(window_id="@1", origin="%1", in_tmux=True)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.hooks._stopped_window", return_value=False),
        patch("vim_ai_follower.hooks.nvim_connect.ready_split_socket", return_value="/c/n.sock"),
        patch(
            "vim_ai_follower.hooks.resolve_nvim_target", return_value=("/c/n.sock", True)
        ) as resolve,
    ):
        hooks._maybe_auto_open(in_tmux, "/tmp/f.txt", cfg)
    resolve.assert_called_once_with("%1", "@1", adopt=False)
    assert not nvim_connect.launch_backoff_active("@1")
    recorded = state.FollowerState.read("@1")
    assert recorded is not None
    assert recorded.target == "/c/n.sock"


def test_a_split_blocked_by_a_silent_listener_backs_off_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The pane a failed split left open still holds the socket while its nvim
    # spins: the next attempt cannot bind it either.
    cfg = _tmux_nvim_auto_open(tmp_path, monkeypatch)
    in_tmux = Session(window_id="@1", origin="%1", in_tmux=True)
    blocked = nvim_connect.SocketPathBlocked("/c/nvim-@1.sock", OSError("a listener"))
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.hooks._stopped_window", return_value=False),
        patch("vim_ai_follower.hooks.resolve_nvim_target", side_effect=blocked),
    ):
        assert hooks._maybe_auto_open(in_tmux, "/tmp/f.txt", cfg) is None
    assert nvim_connect.launch_backoff_active("@1")
    assert "cannot clear /c/nvim-@1.sock: a listener" in capsys.readouterr().err


def test_a_reopen_whose_split_never_answers_keeps_the_old_state_and_backs_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state.FollowerState.set("@1", "nvim", "/tmp/old.sock", origin="%1", on_failure="reopen")
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", side_effect=OSError("dead")),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="absent"),
        patch(
            "vim_ai_follower.hooks.resolve_nvim_target", side_effect=_split_never_answered()
        ) as resolve,
    ):
        assert hooks._get_active_follower("@1") is None
        assert hooks._get_active_follower("@1") is None
    resolve.assert_called_once_with("%1", "@1", adopt=False)
    unchanged = state.FollowerState.read("@1")
    assert unchanged is not None
    assert unchanged.target == "/tmp/old.sock"
    assert nvim_connect.launch_backoff_active("@1")
    assert "nvim did not answer on /c/nvim-@1.sock" in capsys.readouterr().err
