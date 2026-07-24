from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import config, control, state
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


def test_apply_edit_unlocks_the_buffer_only_for_the_animation(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        result = follower.apply_edit("/tmp/f.txt", compute_edit_script("a\n", "b\n"))
    commands = _sent_commands(run)
    # goto_file's defensive preamble runs first
    assert commands[0] == ("Escape", False)
    assert commands[1] == ("Escape", False)
    assert commands[2] == (":tab drop /tmp/f.txt", True)
    assert commands[3] == ("Enter", False)
    assert commands[4] == (":silent! CocDisable", True)
    assert commands[5] == ("Enter", False)
    assert commands[6] == (":setlocal modifiable paste", True)
    assert commands[7] == ("Enter", False)
    assert commands[-2] == (":silent! e! | setlocal nomodifiable nopaste", True)
    assert commands[-1] == ("Enter", False)
    assert result == AnimationResult("completed", 1)


def test_apply_edit_uses_configured_pace_seconds(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", pace_seconds=0.15, window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run"),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.animate.time.sleep") as sleep,
    ):
        follower.apply_edit("/tmp/f.txt", compute_edit_script("a\n", "b\n"))
    sleep.assert_called_with(0.15)


def test_apply_edit_skips_relock_when_interrupted(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
    ):
        result = follower.apply_edit("/tmp/f.txt", compute_edit_script("a\n", "b\n"))
    commands = _sent_commands(run)
    assert result.outcome == "interrupted"
    assert not any(text == ":setlocal nomodifiable nopaste" for text, _ in commands)


def test_apply_edit_relocks_after_pause_and_resume(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    calls = {"n": 0}

    def _pause_then_resume(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "pause" if calls["n"] in (1, 2) else None

    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", side_effect=_pause_then_resume),
    ):
        result = follower.apply_edit("/tmp/f.txt", compute_edit_script("a\n", "b\n"))
    commands = _sent_commands(run)
    assert result.outcome == "completed"
    assert commands[-2] == (":silent! e! | setlocal nomodifiable nopaste", True)


def test_apply_edit_renavigates_to_its_own_tab_on_resume(tmp_path: Path) -> None:
    # The user may have wandered to a different tab during the pause — the
    # resume must re-select the animating file's tab before typing continues,
    # not just once up front via goto_file's initial preamble.
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    calls = {"n": 0}

    def _pause_then_resume(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        return "pause" if calls["n"] in (1, 2) else None

    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", side_effect=_pause_then_resume),
    ):
        result = follower.apply_edit("/tmp/f.txt", compute_edit_script("a\n", "b\n"))
    commands = _sent_commands(run)
    assert result.outcome == "completed"
    tab_drops = [i for i, c in enumerate(commands) if c == (":tab drop /tmp/f.txt", True)]
    # once for the initial goto_file preamble, once more for the resume
    assert len(tab_drops) == 2
    # the second tab drop happens after the pause and before the relock
    relock_index = commands.index((":silent! e! | setlocal nomodifiable nopaste", True))
    assert tab_drops[1] < relock_index


def test_apply_edit_relocks_with_a_silent_disk_sync(tmp_path: Path) -> None:
    # The retyped buffer never "met" the disk — its timestamp doesn't match
    # the file Claude just wrote, causing W11 prompts, `:w` requiring `!`,
    # and LSPs attaching to an ungrounded buffer. `:silent! e!` at relock
    # time reloads the identical content Claude wrote, grounding the buffer
    # with no visible flash.
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        follower.apply_edit("/tmp/f.txt", compute_edit_script("a\n", "b\n"))
    commands = _sent_commands(run)
    assert commands[-2] == (":silent! e! | setlocal nomodifiable nopaste", True)
    assert commands[-1] == ("Enter", False)


def test_show_fresh_relocks_with_a_silent_disk_sync(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        follower.show_fresh("/tmp/f.txt", "a\nb\n")
    commands = _sent_commands(run)
    assert commands[-2] == (":silent! e! | setlocal readonly nomodifiable nopaste", True)
    assert commands[-1] == ("Enter", False)


def test_interrupted_animation_never_sends_a_disk_sync_reload(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
    ):
        result = follower.show_fresh("/tmp/f.txt", "a\nb\n")
    commands = _sent_commands(run)
    assert result.outcome == "interrupted"
    assert not any(text.startswith(":silent! e!") for text, _ in commands)


def test_get_follower_forwards_pace_seconds_for_tmux_backend() -> None:
    follower = get_follower("tmux", "%2", pace_seconds=0.15)
    assert isinstance(follower, TmuxVimFollower)
    assert follower.pace_seconds == 0.15


def test_get_follower_forwards_window_id_for_tmux_backend() -> None:
    follower = get_follower("tmux", "%2", window_id="@7")
    assert isinstance(follower, TmuxVimFollower)
    assert follower.window_id == "@7"


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
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        result = follower.show_fresh("/tmp/f.txt", "a\nb\n")
    commands = _sent_commands(run)
    # _normal_mode sends two prompt-proof Escapes first
    assert commands[0] == ("Escape", False)
    assert commands[1] == ("Escape", False)
    assert commands[2] == (":silent! bwipeout! /tmp/f.txt", True)
    assert commands[3] == ("Enter", False)
    assert commands[4] == (":file /tmp/f.txt", True)
    assert commands[5] == ("Enter", False)
    assert commands[6] == (":filetype detect", True)
    assert commands[7] == ("Enter", False)
    # the renamed-over buffer may be a plugin scratch screen with
    # buftype=nofile — inherited, it makes the user's :w fail with E382
    assert commands[8] == (":setlocal buftype=", True)
    assert commands[9] == ("Enter", False)
    assert commands[10] == (":silent! CocDisable", True)
    assert commands[11] == ("Enter", False)
    assert commands[12] == (":setlocal modifiable paste", True)
    assert commands[13] == ("Enter", False)
    assert commands[14] == (":%d", True)
    assert commands[15] == ("Enter", False)
    assert commands[16] == ("i", True)
    assert commands[-2] == (":silent! e! | setlocal readonly nomodifiable nopaste", True)
    assert commands[-1] == ("Enter", False)
    typed = [text for text, literal in commands if literal]
    assert "a" in typed
    assert "b" in typed
    assert not any(text.startswith(":e ") for text, _ in commands)
    assert result == AnimationResult("completed", 2)


def test_show_fresh_with_empty_content_still_wipes_and_relocks(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        result = follower.show_fresh("/tmp/f.txt", "")
    commands = _sent_commands(run)
    assert commands == [
        ("Escape", False),
        ("Escape", False),
        (":silent! bwipeout! /tmp/f.txt", True),
        ("Enter", False),
        (":file /tmp/f.txt", True),
        ("Enter", False),
        (":filetype detect", True),
        ("Enter", False),
        (":setlocal buftype=", True),
        ("Enter", False),
        (":silent! CocDisable", True),
        ("Enter", False),
        (":setlocal modifiable paste", True),
        ("Enter", False),
        (":%d", True),
        ("Enter", False),
        (":silent! e! | setlocal readonly nomodifiable nopaste", True),
        ("Enter", False),
    ]
    assert result == AnimationResult("completed", 0)


def test_show_fresh_uses_configured_pace_seconds(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", pace_seconds=0.15, window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run"),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.animate.time.sleep") as sleep,
    ):
        follower.show_fresh("/tmp/f.txt", "a\nb\n")
    sleep.assert_called_with(0.15)


def test_show_fresh_skips_relock_when_interrupted(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
    ):
        result = follower.show_fresh("/tmp/f.txt", "a\nb\n")
    commands = _sent_commands(run)
    assert result.outcome == "interrupted"
    assert not any(text == ":setlocal readonly nomodifiable nopaste" for text, _ in commands)


def test_live_pace_reads_current_state_speed() -> None:
    _register_fake_follower("@1", "%2")
    state.FollowerState.update("@1", speed="lento")
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    assert follower._live_pace() == config.SPEED_PACE_SECONDS["lento"]
    state.FollowerState.update("@1", speed="instant")
    assert follower._live_pace() == 0.0


def test_live_pace_falls_back_to_pace_seconds_when_state_is_missing() -> None:
    follower = TmuxVimFollower(pane_id="%2", pace_seconds=0.42, window_id="@1")
    assert follower._live_pace() == 0.42


def test_live_pace_falls_back_to_pace_seconds_when_window_id_is_empty() -> None:
    _register_fake_follower("@1", "%2")
    state.FollowerState.update("@1", speed="lento")
    follower = TmuxVimFollower(pane_id="%2", pace_seconds=0.42, window_id="")
    assert follower._live_pace() == 0.42


def test_resume_at_pace_zero_stays_zero_even_when_state_says_lento(tmp_path: Path) -> None:
    # The pace-0 catch-up (cmd_pause / _handle_hook_post_edit replaying with
    # pace_seconds=0.0) must never re-read live state — otherwise a user who
    # slowed down mid-pause would see the catch-up crawl instead of dumping.
    _register_fake_follower("@1", "%2")
    state.FollowerState.update("@1", speed="lento")
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    pending = control.PendingShowFresh(lines=("a", "b"), pace_seconds=0.0)
    with (
        patch("vim_ai_follower.tmux.subprocess.run"),
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.animate.time.sleep") as sleep,
    ):
        result = follower.resume(pending)
    assert result == AnimationResult("completed", 2)
    sleep.assert_not_called()


def test_resume_apply_edit_replays_remaining_ops_and_relocks(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    pending = control.PendingApplyEdit(ops=[op], pace_seconds=0.0)
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        result = follower.resume(pending)
    commands = _sent_commands(run)
    assert commands[0] == (":silent! CocDisable", True)
    assert commands[1] == ("Enter", False)
    assert commands[2] == (":setlocal modifiable paste", True)
    assert commands[3] == ("Enter", False)
    assert commands[-2] == (":silent! e! | setlocal nomodifiable nopaste", True)
    assert commands[-1] == ("Enter", False)
    assert result == AnimationResult("completed", 1)


def test_resume_show_fresh_replays_remaining_lines_and_relocks_with_readonly(
    tmp_path: Path,
) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    pending = control.PendingShowFresh(lines=("b", "c"), pace_seconds=0.0)
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        result = follower.resume(pending)
    commands = _sent_commands(run)
    assert commands[0] == (":silent! CocDisable", True)
    assert commands[2] == (":setlocal modifiable paste", True)
    assert commands[-2] == (":silent! e! | setlocal readonly nomodifiable nopaste", True)
    assert commands[-1] == ("Enter", False)
    assert result == AnimationResult("completed", 2)


def test_resume_navigates_to_the_pending_files_tab_first(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    pending = control.PendingApplyEdit(ops=[op], pace_seconds=0.0, file_path="/tmp/f.py")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        follower.resume(pending)
    commands = _sent_commands(run)
    assert commands[:4] == [
        ("Escape", False),
        ("Escape", False),
        (":tab drop /tmp/f.py", True),
        ("Enter", False),
    ]


def test_resume_skips_navigation_when_pending_has_no_file_path(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    pending = control.PendingApplyEdit(ops=[op], pace_seconds=0.0)
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        follower.resume(pending)
    commands = _sent_commands(run)
    assert not any(text.startswith(":tab drop") for text, _ in commands)


def test_resume_skips_relock_when_interrupted_again(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
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
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.hand_over()
    assert _sent_commands(run) == [
        (":silent! CocEnable", True),
        ("Enter", False),
        (":setlocal modifiable nopaste", True),
        ("Enter", False),
    ]


def test_goto_file_sends_normal_mode_then_tab_drop() -> None:
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.goto_file("/tmp/a.py")
    assert _sent_commands(run) == [
        ("Escape", False),
        ("Escape", False),
        (":tab drop /tmp/a.py", True),
        ("Enter", False),
    ]


def test_ensure_showing_navigates_by_tab_drop_and_locks() -> None:
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.ensure_showing("/tmp/a.py")
    commands = _sent_commands(run)
    assert commands == [
        ("Escape", False),
        ("Escape", False),
        (":tab drop /tmp/a.py", True),
        ("Enter", False),
        (":setlocal readonly nomodifiable", True),
        ("Enter", False),
    ]
    assert (":e /tmp/a.py", True) not in commands


def test_reload_and_relock_navigates_then_reloads_and_relocks() -> None:
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.reload_and_relock("/tmp/a.py")
    commands = _sent_commands(run)
    assert commands == [
        ("Escape", False),
        ("Escape", False),
        (":tab drop /tmp/a.py", True),
        ("Enter", False),
        (":e!", True),
        ("Enter", False),
        (":setlocal readonly nomodifiable", True),
        ("Enter", False),
    ]


def test_close_tab_wipes_the_buffer_and_never_double_closes() -> None:
    # bwipeout! of a buffer that is a tab's only window ALREADY closes that
    # tab; a follow-up :tabclose then lands on whichever neighbor received
    # focus and closes an innocent tab (live eviction bug, 2026-07-15).
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.close_tab("/tmp/old.py")
    commands = _sent_commands(run)
    drop_index = commands.index((":tab drop /tmp/old.py", True))
    texts_after_drop = [text for text, _ in commands[drop_index:]]
    assert ":silent! bwipeout! /tmp/old.py" in texts_after_drop
    assert not any("tabclose" in text for text, _ in commands)


def test_show_fresh_in_new_tab_opens_tab_before_renaming(
    sent: list[str], follower: TmuxVimFollower
) -> None:
    follower.show_fresh("/tmp/new.py", "line1\n", in_new_tab=True)
    wipe = sent.index("text:::silent! bwipeout! /tmp/new.py")
    tabnew = sent.index("text:::tabnew")
    rename = sent.index("text:::file /tmp/new.py")
    assert wipe < tabnew < rename


def test_show_fresh_default_renames_in_place(sent: list[str], follower: TmuxVimFollower) -> None:
    follower.show_fresh("/tmp/new.py", "line1\n")
    # Vim commands carry their own leading ":", so the recorded entry has
    # three colons — "text::tabnew" would never match anything.
    assert "text:::tabnew" not in sent
