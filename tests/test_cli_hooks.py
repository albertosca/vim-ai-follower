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

from vim_ai_follower import (
    cache,
    cli,
    config,
    control,
    hooks,
    keybindings,
    snapshot,
    state,
    writer_cue,
)
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.session import Session


def _goto(path: object) -> str:
    """The exact Ex line the tmux backend's goto_file sends for `path`.

    Written out here instead of imported from tmux_vim._GOTO_FILE on
    purpose: these assertions exist to catch an unintended change to that
    constant, which importing it would hide. The `:try`/`:catch` wrapper
    swallows E37 (a modified target buffer would otherwise leave a blocking
    hit-enter prompt in the pane) and nothing else; the `SwapExists` hook
    around it answers the swap-file ATTENTION dialog with `(E)dit anyway`
    and is torn down in `finally`."""
    return (
        ':exe "augroup vim_ai_follower_swap"'
        " | exe \"autocmd SwapExists * ++once let v:swapchoice = 'e'\""
        ' | exe "augroup END"'
        rf" | try | tab drop {path} | catch /^Vim\%((\a\+)\)\=:E37:/"
        ' | finally | exe "autocmd! vim_ai_follower_swap"'
        ' | exe "augroup! vim_ai_follower_swap" | endtry'
    )


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

    assert _literal_sends(run) == [_goto(target), ":setlocal readonly nomodifiable"]


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

    assert _literal_sends(run) == [_goto(target), ":setlocal readonly nomodifiable"]


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
        _goto(target),
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
        _goto(target),
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
    assert _goto(a) in sends
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
    nvim.funcs.bufnr.return_value = 7  # the buffer number bufnr(str(a)) resolves to
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("pynvim.attach", return_value=nvim),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.open_files == (str(b), str(c))
    # close_tab looks the evicted file (a) up via bufnr() (nvim's own path
    # canonicalization, not a raw string compare) then wipes it by number.
    nvim.funcs.bufnr.assert_any_call(str(a))
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


def test_auto_open_selects_nvim_backend_and_persists_launched(
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
            return_value=("/tmp/nvim.sock", True),
        ) as resolve,
        # The follower's own connection (show_fresh) is mocked out — this test
        # only asserts the backend selection and persisted state.
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=MagicMock()),
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
    assert ":e!" not in sends  # never the instant full-file reload
    # the replay typed the file's lines after the des-interrupt
    assert "a" in sends and "b" in sends
    # and relocked (fresh-retype lock) when the replay completed
    assert ":silent! e! | setlocal readonly nomodifiable nopaste" in sends
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
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}

    with (
        patch("vim_ai_follower.hooks.get_follower", return_value=fake),
        patch("pynvim.attach", return_value=MagicMock()),
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
    assert ":setlocal modifiable nopaste" in sends  # handed over, unlocked
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
    attach.assert_called_once_with("socket", path="/tmp/x.sock")
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
    with patch("pynvim.attach", return_value=MagicMock()):
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
    ):
        result = hooks._maybe_auto_open(standalone, "/tmp/f.txt", cfg)

    launch.assert_called_once_with("term-x")
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
    ):
        result = hooks._maybe_auto_open(in_tmux, "/tmp/f.txt", cfg)

    launch.assert_called_once_with("@1")
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
