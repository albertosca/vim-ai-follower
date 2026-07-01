from __future__ import annotations

from unittest.mock import MagicMock, patch

from vim_ai_follower.tmux import TmuxPane, TmuxSession


def test_pane_exists_true_when_pane_id_in_list() -> None:
    pane = TmuxPane(pane_id="%3")
    fake_result = MagicMock(stdout="%1\n%2\n%3\n")
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=fake_result) as run:
        assert pane.exists() is True
    run.assert_called_once_with(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_pane_exists_false_when_pane_id_missing() -> None:
    pane = TmuxPane(pane_id="%99")
    fake_result = MagicMock(stdout="%1\n%2\n")
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=fake_result):
        assert pane.exists() is False


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


def test_kill_calls_tmux_kill_pane() -> None:
    pane = TmuxPane(pane_id="%3")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        pane.kill()
    run.assert_called_once_with(["tmux", "kill-pane", "-t", "%3"], check=False)


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


def test_session_from_env_returns_none_without_tmux_pane() -> None:
    assert TmuxSession.from_env({}) is None


def test_session_from_env_resolves_session_id() -> None:
    fake_result = MagicMock(returncode=0, stdout="$3\n")
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=fake_result) as run:
        session = TmuxSession.from_env({"TMUX_PANE": "%1"})
    assert session is not None
    assert session.session_id == "$3"
    run.assert_called_once_with(
        ["tmux", "display-message", "-p", "-t", "%1", "#{session_id}"],
        capture_output=True,
        text=True,
        check=False,
    )


def test_session_from_env_returns_none_when_tmux_fails() -> None:
    fake_result = MagicMock(returncode=1, stdout="")
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=fake_result):
        assert TmuxSession.from_env({"TMUX_PANE": "%1"}) is None
