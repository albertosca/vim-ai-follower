from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower import tmux
from vim_ai_follower.tmux import TmuxPane, TmuxWindow, adopt_target


def test_send_text_calls_tmux_send_keys_literal() -> None:
    pane = TmuxPane(pane_id="%3")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        pane.send_text("hello $world")
    run.assert_called_once_with(
        ["tmux", "send-keys", "-t", "%3", "-l", "--", "hello $world"],
        check=True,
    )


def test_send_key_calls_tmux_send_keys_named() -> None:
    pane = TmuxPane(pane_id="%3")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        pane.send_key("Enter")
    run.assert_called_once_with(["tmux", "send-keys", "-t", "%3", "Enter"], check=True)


@pytest.mark.real_send_line
def test_send_line_sends_the_text_and_its_enter_in_one_tmux_call() -> None:
    """One `send-keys` per typed line, the Enter (a CR) in the same write:
    with the Enter in a second subprocess Vim drew the line on its command
    line and waited 3-64 ms for it (task 23's echo measurement)."""
    pane = TmuxPane(pane_id="%3")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        pane.send_line(":call Fn('x')")
    run.assert_called_once_with(
        ["tmux", "send-keys", "-t", "%3", "-l", "--", ":call Fn('x')\r"],
        check=True,
    )


def test_kill_calls_tmux_kill_pane() -> None:
    pane = TmuxPane(pane_id="%3")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        pane.kill()
    run.assert_called_once_with(["tmux", "kill-pane", "-t", "%3"], check=False)


def test_running_command_returns_command_for_existing_pane() -> None:
    pane = TmuxPane(pane_id="%3")
    fake_result = MagicMock(stdout="%1 zsh\n%3 vim\n")
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=fake_result) as run:
        assert pane.running_command() == "vim"
    run.assert_called_once_with(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_current_command}"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_running_command_returns_none_for_missing_pane() -> None:
    pane = TmuxPane(pane_id="%99")
    fake_result = MagicMock(stdout="%1 zsh\n%3 vim\n")
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=fake_result):
        assert pane.running_command() is None


def test_pane_pid_returns_int_for_existing_pane() -> None:
    pane = TmuxPane(pane_id="%3")
    fake_result = MagicMock(stdout="4242\n")
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=fake_result) as run:
        assert pane.pane_pid() == 4242
    run.assert_called_once_with(
        ["tmux", "display-message", "-p", "-t", "%3", "#{pane_pid}"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_pane_pid_returns_none_on_unparseable_output() -> None:
    pane = TmuxPane(pane_id="%3")
    fake_result = MagicMock(stdout="\n")
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=fake_result):
        assert pane.pane_pid() is None


def test_split_from_returns_pane_with_new_id() -> None:
    fake_result = MagicMock(stdout="%42\n")
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=fake_result) as run:
        pane = TmuxPane.split_from("%1", "vim")
    assert pane.pane_id == "%42"
    run.assert_called_once_with(
        ["tmux", "split-window", "-h", "-t", "%1", "-P", "-F", "#{pane_id}", "vim"],
        capture_output=True,
        text=True,
        check=True,
    )


def test_window_from_env_returns_none_without_tmux_pane() -> None:
    assert TmuxWindow.from_env({}) is None


def test_window_from_env_resolves_window_id() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="@3\n")
        window = TmuxWindow.from_env({"TMUX_PANE": "%1"})
    assert window is not None
    assert window.window_id == "@3"
    mock_run.assert_called_once_with(
        ["tmux", "display-message", "-p", "-t", "%1", "#{window_id}"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_window_from_env_returns_none_when_tmux_fails() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert TmuxWindow.from_env({"TMUX_PANE": "%1"}) is None


def test_window_from_env_returns_none_when_window_id_is_empty() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="\n")
        assert TmuxWindow.from_env({"TMUX_PANE": "%1"}) is None


def test_window_panes_lists_only_this_window() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="%1 zsh\n%2 vim\n")
        panes = TmuxPane(pane_id="%1").window_panes()
    assert panes == [("%1", "zsh"), ("%2", "vim")]
    assert run.call_args[0][0][:4] == ["tmux", "list-panes", "-t", "%1"]


def test_set_zoomed_only_toggles_when_state_differs() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="0\n")
        TmuxPane(pane_id="%1").set_zoomed(True)
        assert ["tmux", "resize-pane", "-Z", "-t", "%1"] in [c[0][0] for c in run.call_args_list]
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="1\n")
        TmuxPane(pane_id="%1").set_zoomed(True)
        assert ["tmux", "resize-pane", "-Z", "-t", "%1"] not in [
            c[0][0] for c in run.call_args_list
        ]


def test_pane_title_round_trip() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="my title\n")
        title = TmuxPane(pane_id="%1").title()
    assert title == "my title"
    assert run.call_args[0][0] == [
        "tmux",
        "display-message",
        "-p",
        "-t",
        "%1",
        "#{pane_title}",
    ]
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        TmuxPane(pane_id="%1").set_title("waiting")
    assert run.call_args[0][0] == ["tmux", "select-pane", "-t", "%1", "-T", "waiting"]


def test_window_option_get_set_and_unset() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="top\n")
        value = TmuxPane(pane_id="%1").window_option("pane-border-status")
    assert value == "top"
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="\n")
        assert TmuxPane(pane_id="%1").window_option("pane-border-status") is None
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        TmuxPane(pane_id="%1").set_window_option("pane-border-status", "top")
    assert run.call_args[0][0] == [
        "tmux",
        "set-option",
        "-w",
        "-t",
        "%1",
        "pane-border-status",
        "top",
    ]
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        TmuxPane(pane_id="%1").set_window_option("pane-border-status", None)
    assert run.call_args[0][0] == [
        "tmux",
        "set-option",
        "-wu",
        "-t",
        "%1",
        "pane-border-status",
    ]


def _panes_run(listing: str) -> MagicMock:
    return MagicMock(returncode=0, stdout=listing)


def test_adopt_target_requires_exactly_one_vim_pane() -> None:
    # One vim: adopt it. Two vims: ambiguous — never guess which Vim the
    # user meant; the caller falls back to a dedicated split and says why.
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        run.return_value = _panes_run("%1 zsh\n%7 vim\n")
        assert adopt_target("%1") == "%7"
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        run.return_value = _panes_run("%1 zsh\n%7 vim\n%9 vim\n")
        assert adopt_target("%1") is None
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        run.return_value = _panes_run("%1 zsh\n%2 zsh\n")
        assert adopt_target("%1") is None


def test_set_border_color_sets_both_pane_border_styles() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        TmuxPane(pane_id="%2").set_border_color("colour203")
    calls = [c.args[0] for c in run.call_args_list]
    assert [
        "tmux",
        "set-option",
        "-p",
        "-t",
        "%2",
        "pane-border-style",
        "fg=colour203",
    ] in calls
    assert [
        "tmux",
        "set-option",
        "-p",
        "-t",
        "%2",
        "pane-active-border-style",
        "fg=colour203",
    ] in calls


def test_set_border_color_none_unsets_both_styles() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        TmuxPane(pane_id="%2").set_border_color(None)
    calls = [c.args[0] for c in run.call_args_list]
    assert ["tmux", "set-option", "-pu", "-t", "%2", "pane-border-style"] in calls
    assert ["tmux", "set-option", "-pu", "-t", "%2", "pane-active-border-style"] in calls


@pytest.mark.parametrize("after_seconds", [0.0, 1.6])
def test_a_popup_is_spawned_detached_from_the_callers_output(after_seconds: float) -> None:
    """Review of T11, m1: display-popup -E (and the delayed shell before it)
    lives as long as the popup, and inherited the hook's stdout/stderr — a
    reader waiting for end-of-output waited for the popup too."""
    with patch("vim_ai_follower.tmux.subprocess.Popen") as popen:
        tmux.show_popup("%2", "Paused", after_seconds=after_seconds)
    kwargs = popen.call_args.kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True


def test_a_delayed_popup_does_not_hold_the_callers_pipe(tmp_path: Path) -> None:
    """The real thing: a process that asks for the delayed correction popup
    and exits must reach end-of-output at once, not when the popup would
    close. No tmux server is needed (the client fails on its own); the
    private TMUX_TMPDIR and no TMUX keep it off the user's server."""
    environment = {
        key: value for key, value in os.environ.items() if key not in ("TMUX", "TMUX_PANE")
    }
    environment["TMUX_TMPDIR"] = str(tmp_path)
    script = (
        "from vim_ai_follower import tmux; "
        "tmux.show_popup('%2', 'Nothing to pause', after_seconds=2.5)"
    )
    started = time.monotonic()
    child = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        timeout=10,
        check=False,
    )
    elapsed = time.monotonic() - started
    assert child.returncode == 0, child.stderr
    assert elapsed < 2.0, f"end-of-output came {elapsed:.2f} s after the start"
