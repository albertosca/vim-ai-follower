from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from vim_ai_follower import control
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends import get_follower
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import EditOp, compute_edit_script


def test_is_alive_true_when_vim_is_running_in_pane() -> None:
    follower = TmuxVimFollower(pane_id="%2")
    fake_result = MagicMock(stdout="%1 zsh\n%2 vim\n")
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=fake_result):
        assert follower.is_alive() is True


def test_is_alive_false_when_pane_is_gone() -> None:
    follower = TmuxVimFollower(pane_id="%2")
    fake_result = MagicMock(stdout="%1 zsh\n")
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=fake_result):
        assert follower.is_alive() is False


def test_is_alive_false_when_pane_survived_but_vim_crashed() -> None:
    follower = TmuxVimFollower(pane_id="%2")
    fake_result = MagicMock(stdout="%1 zsh\n%2 zsh\n")
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=fake_result):
        assert follower.is_alive() is False


def _sent_commands(run_mock: MagicMock) -> list[tuple[str, bool]]:
    """(text, literal) pairs, in order, for send-keys calls targeting %2."""
    commands = []
    for call in run_mock.call_args_list:
        cmd = call.args[0]
        if cmd[:4] != ["tmux", "send-keys", "-t", "%2"]:
            continue
        if "-l" in cmd:
            commands.append((cmd[6], True))
        else:
            commands.append((cmd[4], False))
    return commands


def test_ensure_showing_opens_file_then_locks_the_buffer() -> None:
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.ensure_showing("/tmp/f.txt")
    assert _sent_commands(run) == [
        (":e /tmp/f.txt", True),
        ("Enter", False),
        (":setlocal readonly nomodifiable", True),
        ("Enter", False),
    ]


def test_apply_edit_unlocks_the_buffer_only_for_the_animation(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", session_id="$1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        result = follower.apply_edit(compute_edit_script("a\n", "b\n"))
    commands = _sent_commands(run)
    assert commands[0] == (":setlocal modifiable paste", True)
    assert commands[1] == ("Enter", False)
    assert commands[-2] == (":setlocal nomodifiable nopaste", True)
    assert commands[-1] == ("Enter", False)
    assert result == AnimationResult("completed", 1)


def test_apply_edit_uses_configured_pace_seconds(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", pace_seconds=0.15, session_id="$1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run"),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.animate.time.sleep") as sleep,
    ):
        follower.apply_edit(compute_edit_script("a\n", "b\n"))
    sleep.assert_called_with(0.15)


def test_apply_edit_skips_relock_when_interrupted(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", session_id="$1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
    ):
        result = follower.apply_edit(compute_edit_script("a\n", "b\n"))
    commands = _sent_commands(run)
    assert result.outcome == "interrupted"
    assert not any(text == ":setlocal nomodifiable nopaste" for text, _ in commands)


def test_apply_edit_relocks_when_paused(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", session_id="$1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value="pause"),
    ):
        result = follower.apply_edit(compute_edit_script("a\n", "b\n"))
    commands = _sent_commands(run)
    assert result.outcome == "paused"
    assert commands[-2] == (":setlocal nomodifiable nopaste", True)


def test_get_follower_forwards_pace_seconds_for_tmux_backend() -> None:
    follower = get_follower("tmux", "%2", pace_seconds=0.15)
    assert isinstance(follower, TmuxVimFollower)
    assert follower.pace_seconds == 0.15


def test_get_follower_forwards_session_id_for_tmux_backend() -> None:
    follower = get_follower("tmux", "%2", session_id="$7")
    assert isinstance(follower, TmuxVimFollower)
    assert follower.session_id == "$7"


def test_show_fresh_renames_current_buffer_without_ever_loading_the_real_file(
    tmp_path: Path,
) -> None:
    # No `:e` here on purpose: loading the real file would flash its final
    # content on screen before the wipe+retype, spoiling the "watch it type"
    # effect. Instead the current buffer is wiped and renamed in place.
    #
    # `:filetype detect` must run BEFORE 'paste' is enabled: loading the
    # filetype's indent/ftplugin scripts can turn cindent/smartindent/
    # indentexpr back on, and 'paste' only suppresses whatever was active
    # at the moment it's set — anything enabled afterwards still fires.
    follower = TmuxVimFollower(pane_id="%2", session_id="$1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        result = follower.show_fresh("/tmp/f.txt", "a\nb\n")
    commands = _sent_commands(run)
    assert commands[0] == (":silent! bwipeout! /tmp/f.txt", True)
    assert commands[1] == ("Enter", False)
    assert commands[2] == (":file /tmp/f.txt", True)
    assert commands[3] == ("Enter", False)
    assert commands[4] == (":filetype detect", True)
    assert commands[5] == ("Enter", False)
    # the renamed-over buffer may be a plugin scratch screen with
    # buftype=nofile — inherited, it makes the user's :w fail with E382
    assert commands[6] == (":setlocal buftype=", True)
    assert commands[7] == ("Enter", False)
    assert commands[8] == (":setlocal modifiable paste", True)
    assert commands[9] == ("Enter", False)
    assert commands[10] == (":%d", True)
    assert commands[11] == ("Enter", False)
    assert commands[12] == ("i", True)
    assert commands[-2] == (":setlocal readonly nomodifiable nopaste", True)
    assert commands[-1] == ("Enter", False)
    typed = [text for text, literal in commands if literal]
    assert "a" in typed
    assert "b" in typed
    assert not any(text.startswith(":e ") for text, _ in commands)
    assert result == AnimationResult("completed", 2)


def test_show_fresh_with_empty_content_still_wipes_and_relocks(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", session_id="$1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        result = follower.show_fresh("/tmp/f.txt", "")
    commands = _sent_commands(run)
    assert commands == [
        (":silent! bwipeout! /tmp/f.txt", True),
        ("Enter", False),
        (":file /tmp/f.txt", True),
        ("Enter", False),
        (":filetype detect", True),
        ("Enter", False),
        (":setlocal buftype=", True),
        ("Enter", False),
        (":setlocal modifiable paste", True),
        ("Enter", False),
        (":%d", True),
        ("Enter", False),
        (":setlocal readonly nomodifiable nopaste", True),
        ("Enter", False),
    ]
    assert result == AnimationResult("completed", 0)


def test_show_fresh_uses_configured_pace_seconds(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", pace_seconds=0.15, session_id="$1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run"),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.animate.time.sleep") as sleep,
    ):
        follower.show_fresh("/tmp/f.txt", "a\nb\n")
    sleep.assert_called_with(0.15)


def test_show_fresh_skips_relock_when_interrupted(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", session_id="$1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
    ):
        result = follower.show_fresh("/tmp/f.txt", "a\nb\n")
    commands = _sent_commands(run)
    assert result.outcome == "interrupted"
    assert not any(text == ":setlocal readonly nomodifiable nopaste" for text, _ in commands)


def test_resume_apply_edit_replays_remaining_ops_and_relocks(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", session_id="$1")
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    pending = control.PendingApplyEdit(ops=[op], pace_seconds=0.0)
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        result = follower.resume(pending)
    commands = _sent_commands(run)
    assert commands[0] == (":setlocal modifiable paste", True)
    assert commands[1] == ("Enter", False)
    assert commands[-2] == (":setlocal nomodifiable nopaste", True)
    assert commands[-1] == ("Enter", False)
    assert result == AnimationResult("completed", 1)


def test_resume_show_fresh_replays_remaining_lines_and_relocks_with_readonly(
    tmp_path: Path,
) -> None:
    follower = TmuxVimFollower(pane_id="%2", session_id="$1")
    pending = control.PendingShowFresh(lines=("b", "c"), pace_seconds=0.0)
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        result = follower.resume(pending)
    commands = _sent_commands(run)
    assert commands[0] == (":setlocal modifiable paste", True)
    assert commands[-2] == (":setlocal readonly nomodifiable nopaste", True)
    assert commands[-1] == ("Enter", False)
    assert result == AnimationResult("completed", 2)


def test_resume_skips_relock_when_interrupted_again(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", session_id="$1")
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    pending = control.PendingApplyEdit(ops=[op], pace_seconds=0.0)
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
    ):
        result = follower.resume(pending)
    commands = _sent_commands(run)
    assert result.outcome == "interrupted"
    assert not any(text == ":setlocal nomodifiable nopaste" for text, _ in commands)


def test_hand_over_unlocks_the_buffer() -> None:
    follower = TmuxVimFollower(pane_id="%2", session_id="$1")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.hand_over()
    assert _sent_commands(run) == [
        (":setlocal modifiable nopaste", True),
        ("Enter", False),
    ]
