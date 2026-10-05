from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import (
    call_spelled,
    define_line,
    goto_spelled,
    landed_line,
    rename_spelled,
    resolve_typed_paths,
    wipe_spelled,
)
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import cache, config, control, state
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends import get_follower
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower, _case_folds
from vim_ai_follower.diff import EditOp, compute_edit_script


def _goto(path: str) -> str:
    """The call goto_file sends for `path` (helpers.goto_spelled: spelled out
    there, never imported from tmux_vim)."""
    return goto_spelled(path)


# The lines that only call the functions (tmux_vim._VIM_SCRIPT), spelled out
# for the same reason as _goto. The define line comes first in every entry
# point that may be the first thing a Vim hears.
_DEFINE = define_line()
# ensure_showing's clean-only disk re-read and the discarding one, in a
# dedicated Vim (an adopted Vim's carry adopted=1: they note the user's
# readonly first).
_RELOAD_IF_CLEAN = call_spelled("reload", 0, 0)
_RELOAD_DISCARDING = call_spelled("reload", 1, 0)
# The completion relocks, with their `:e!` disk sync under the scoped
# (E)dit-anyway answer.
_RELOCK_SYNCED = call_spelled("relock", 0, 0)
_RELOCK_READONLY_SYNCED = call_spelled("relock", 1, 0)
# The animation unlock, spelled out for the same reason as _goto.
_UNLOCK_FOR_ANIMATION = ":setlocal noreadonly modifiable paste"
# An ADOPTED Vim's readonly bookkeeping (s:note_user_readonly): the unlock
# that notes the user's readonly and claims the option, the lock's claim, and
# the restore every animation exit sends (its answer for pane %2).
_ADOPTED_UNLOCK = call_spelled("unlock")
_ADOPTED_LOCK_READONLY = ":setlocal readonly nomodifiable | let b:vaf_ro_ours = 1"


def _restore() -> str:
    return call_spelled("restore_readonly", 2)


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
            commands.append((resolve_typed_paths(cmd[6]), True))
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
    # goto_file's defensive preamble runs first: normal mode, the functions
    # (sourced only when the Vim lacks them), then the navigation itself
    assert commands[0] == ("Escape", False)
    assert commands[1] == ("Escape", False)
    assert commands[2:4] == [(_DEFINE, True), ("Enter", False)]
    assert commands[4] == (_goto("/tmp/f.txt"), True)
    assert commands[5] == ("Enter", False)
    # ...and nothing more until Vim confirms it landed there
    assert commands[6:10] == [
        ("Escape", False),
        ("Escape", False),
        (landed_line(), True),
        ("Enter", False),
    ]
    assert commands[10] == (":silent! CocDisable", True)
    assert commands[11] == ("Enter", False)
    assert commands[12] == (_UNLOCK_FOR_ANIMATION, True)
    assert commands[13] == ("Enter", False)
    assert commands[-2:] == [(_RELOCK_SYNCED, True), ("Enter", False)]
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
    assert (_RELOCK_SYNCED, True) not in commands
    # A dedicated follower's readonly is never the user's: no bookkeeping.
    assert not any("b:vaf_" in text for text, _ in commands)


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
    assert commands[-2] == (_RELOCK_SYNCED, True)


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
    tab_drops = [i for i, c in enumerate(commands) if c == (_goto("/tmp/f.txt"), True)]
    # once for the initial goto_file preamble, once more for the resume
    assert len(tab_drops) == 2
    # the second tab drop happens after the pause and before the relock
    relock_index = commands.index((_RELOCK_SYNCED, True))
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
    assert commands[-2:] == [(_RELOCK_SYNCED, True), ("Enter", False)]


def test_show_fresh_relocks_with_a_silent_disk_sync(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        follower.show_fresh("/tmp/f.txt", "a\nb\n")
    commands = _sent_commands(run)
    assert commands[-2:] == [(_RELOCK_READONLY_SYNCED, True), ("Enter", False)]


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
    # no disk sync of any shape once the animation has started (the synced
    # relock carries it as `silent! edit!`, a reload as `edit`); the
    # rename's read-then-clear runs before the unlock, on a buffer nothing
    # has been typed into yet
    started = commands.index((_UNLOCK_FOR_ANIMATION, True))
    assert not any(
        "e!" in text or "'relock'" in text or "'reload'" in text for text, _ in commands[started:]
    )


def test_get_follower_forwards_pace_seconds_for_tmux_backend() -> None:
    follower = get_follower("tmux", "%2", pace_seconds=0.15)
    assert isinstance(follower, TmuxVimFollower)
    assert follower.pace_seconds == 0.15


def test_get_follower_forwards_window_id_for_tmux_backend() -> None:
    follower = get_follower("tmux", "%2", window_id="@7")
    assert isinstance(follower, TmuxVimFollower)
    assert follower.window_id == "@7"


def test_show_fresh_renames_in_place_and_reads_the_file_only_on_the_rename_line(
    tmp_path: Path,
) -> None:
    # No separate `:e` on purpose: loading the real file would flash its
    # final content on screen before the wipe+retype, spoiling the "watch it
    # type" effect. Instead the current buffer is wiped and renamed in place,
    # and the one read it gets (which clears the rename's "not edited" mark,
    # the E13 on a plain `:w` after an interrupt) sits on the rename line and
    # is cleared on that same line, where Vim never redraws in between.
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
    assert commands[2:4] == [(_DEFINE, True), ("Enter", False)]
    assert commands[4] == (_wipe("/tmp/f.txt"), True)
    assert commands[5] == ("Enter", False)
    # The rename (s:rename) opts swap out BEFORE renaming: renaming a
    # swap-enabled buffer runs the swap check, and a live Vim holding the
    # file's swap made `:file` raise E325 (see
    # tests/test_e2e_battery_tranche2.py). It also resets buftype (a plugin
    # scratch screen's buftype=nofile, inherited, made the user's :w fail
    # with E382) BEFORE its read, which only reads a file into a regular
    # buffer (test_tmux_landing pins the function's text).
    assert commands[6] == (_rename("/tmp/f.txt"), True)
    assert commands[7] == ("Enter", False)
    # ...and nothing more until Vim confirms the rename landed
    assert commands[8:12] == [
        ("Escape", False),
        ("Escape", False),
        (landed_line(), True),
        ("Enter", False),
    ]
    assert commands[12] == (":filetype detect", True)
    assert commands[13] == ("Enter", False)
    assert commands[14] == (":silent! CocDisable", True)
    assert commands[15] == ("Enter", False)
    assert commands[16] == (_UNLOCK_FOR_ANIMATION, True)
    assert commands[17] == ("Enter", False)
    assert commands[18] == (":%d", True)
    assert commands[19] == ("Enter", False)
    assert commands[20] == ("i", True)
    assert commands[-2:] == [(_RELOCK_READONLY_SYNCED, True), ("Enter", False)]
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
        (_DEFINE, True),
        ("Enter", False),
        (_wipe("/tmp/f.txt"), True),
        ("Enter", False),
        (_rename("/tmp/f.txt"), True),
        ("Enter", False),
        ("Escape", False),
        ("Escape", False),
        (landed_line(), True),
        ("Enter", False),
        (":filetype detect", True),
        ("Enter", False),
        (":silent! CocDisable", True),
        ("Enter", False),
        (_UNLOCK_FOR_ANIMATION, True),
        ("Enter", False),
        (":%d", True),
        ("Enter", False),
        (_RELOCK_READONLY_SYNCED, True),
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
    # No file to navigate to, so the functions the relock calls are made
    # sure of on their own.
    assert commands[0:2] == [(_DEFINE, True), ("Enter", False)]
    assert commands[2] == (":silent! CocDisable", True)
    assert commands[3] == ("Enter", False)
    assert commands[4] == (_UNLOCK_FOR_ANIMATION, True)
    assert commands[5] == ("Enter", False)
    assert commands[-2:] == [(_RELOCK_SYNCED, True), ("Enter", False)]
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
    assert commands[0] == (_DEFINE, True)
    assert commands[2] == (":silent! CocDisable", True)
    assert commands[4] == (_UNLOCK_FOR_ANIMATION, True)
    assert commands[-2:] == [(_RELOCK_READONLY_SYNCED, True), ("Enter", False)]
    assert result == AnimationResult("completed", 2)


@pytest.mark.parametrize(
    ("pending", "relock"),
    [
        (
            control.PendingApplyEdit(
                ops=[EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))],
                pace_seconds=0.0,
            ),
            ":setlocal nomodifiable nopaste",
        ),
        (
            control.PendingShowFresh(lines=("b", "c"), pace_seconds=0.0),
            ":setlocal readonly nomodifiable nopaste",
        ),
    ],
    ids=["apply_edit", "show_fresh"],
)
def test_resume_without_reload_relocks_without_reading_disk(
    pending: control.PendingApplyEdit | control.PendingShowFresh, relock: str, tmp_path: Path
) -> None:
    # The catch-up's end state is the NEXT edit's base, and by hook post disk
    # already holds that next edit: an `:e!` here would load the finished file.
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        result = follower.resume(pending, reload=False)
    commands = _sent_commands(run)
    assert commands[-2:] == [(relock, True), ("Enter", False)]
    assert not any("e!" in text or "'relock'" in text for text, _ in commands)
    assert result.outcome == "completed"


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
    assert commands[:10] == [
        ("Escape", False),
        ("Escape", False),
        (_DEFINE, True),
        ("Enter", False),
        (_goto("/tmp/f.py"), True),
        ("Enter", False),
        ("Escape", False),
        ("Escape", False),
        (landed_line(), True),
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
    # The navigation is a call to s:goto; the `:tab drop` is inside it.
    assert not any("'goto'" in text for text, _ in commands)


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
        (_DEFINE, True),
        ("Enter", False),
        (_goto("/tmp/a.py"), True),
        ("Enter", False),
        ("Escape", False),
        ("Escape", False),
        (landed_line(), True),
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
        (_DEFINE, True),
        ("Enter", False),
        (_goto("/tmp/a.py"), True),
        ("Enter", False),
        ("Escape", False),
        ("Escape", False),
        (landed_line(), True),
        ("Enter", False),
        (_RELOAD_IF_CLEAN, True),
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
        (_DEFINE, True),
        ("Enter", False),
        (_goto("/tmp/a.py"), True),
        ("Enter", False),
        ("Escape", False),
        ("Escape", False),
        (landed_line(), True),
        ("Enter", False),
        (_RELOAD_DISCARDING, True),
        ("Enter", False),
        (":setlocal readonly nomodifiable", True),
        ("Enter", False),
    ]


def test_reload_from_disk_discards_under_the_swap_answer_and_locks() -> None:
    # The catch-up grounding (hooks._ground_caught_up_buffer): the buffer holds
    # only follower text, so `edit!` discards it; the same scoped (E)dit-anyway
    # answer as every disk-reading line, and `silent` against a 49-column
    # hit-enter prompt.
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.reload_from_disk("/tmp/a.py")
    assert _sent_commands(run) == [
        ("Escape", False),
        ("Escape", False),
        (_DEFINE, True),
        ("Enter", False),
        (_goto("/tmp/a.py"), True),
        ("Enter", False),
        ("Escape", False),
        ("Escape", False),
        (landed_line(), True),
        ("Enter", False),
        (_RELOAD_DISCARDING, True),
        ("Enter", False),
        (":setlocal readonly nomodifiable", True),
        ("Enter", False),
    ]


def _wipe(path: str) -> str:
    """The exact Ex line an eviction sends to wipe `path`'s buffer
    (helpers.wipe_spelled)."""
    return wipe_spelled(path)


def _rename(path: str, *, adopted: bool = False, in_new_tab: bool = False) -> str:
    """show_fresh's rename-in-place line (helpers.rename_spelled)."""
    return rename_spelled(path, adopted=adopted, in_new_tab=in_new_tab)


def test_close_tab_wipes_by_buffer_number_and_never_double_closes() -> None:
    """The exact Ex traffic of one eviction.

    Two separate regressions are pinned here, both measured on a real
    tmux+vim on 2026-09-22:

    1. The wipe must resolve a buffer NUMBER. `:bwipeout! {path}` treats its
       argument as a buffer-name PATTERN, so a path containing `[`, `]`, `{`
       or `}` matches nothing and `:silent!` eats the E94 — the "evicted"
       buffer and its tab both survive and max_tabs stops capping anything.
    2. There must be no goto_file preamble. `:tab drop` OPENS a tab for a
       path Vim does not already hold, so the old pairing turned a missed
       wipe into a tab ADDED rather than removed.

    And the 2026-07-15 rule still stands: never `:tabclose` after the wipe —
    focus lands on a neighbour and the "safety" close eats an innocent tab.
    """
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.close_tab("/tmp/old.py")
    commands = _sent_commands(run)
    # Two Escapes to reach Normal mode, then the wipe, then Enter. Nothing
    # else: no navigation, so nothing can open a tab on the way in.
    assert commands == [
        ("Escape", False),
        ("Escape", False),
        (_DEFINE, True),
        ("Enter", False),
        (_wipe("/tmp/old.py"), True),
        ("Enter", False),
    ]
    assert not any("tab drop" in text for text, _ in commands)
    assert not any("tabclose" in text for text, _ in commands)


def test_close_tab_never_sends_the_path_as_a_bwipeout_pattern() -> None:
    """The regression in the form it actually shipped in: a path whose
    characters are buffer-name pattern metacharacters. `app/[slug]/page.tsx`
    is an ordinary Next.js route, and `:bwipeout! .../[slug]/page.tsx` reads
    `[slug]` as a character class, matching no buffer at all.

    Asserting the path is absent from the command line in bare form is the
    load-bearing half: it must reach Vim only inside a string literal, where
    no character is special."""
    path = "/tmp/app/[slug]/page.tsx"
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.close_tab(path)
    texts = [text for text, literal in _sent_commands(run) if literal]
    assert texts == [_DEFINE, _wipe(path)]
    assert not any(f"bwipeout! {path}" in text for text in texts)


def test_close_tab_hands_an_apostrophe_in_the_path_over_intact() -> None:
    """The path reaches Vim through a handle file, never a Vim string literal
    on the line: an apostrophe (which a literal would have to double, or
    terminate early) is not on the command line at all, and the handle
    holds it verbatim."""
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.close_tab("/tmp/it's/a.py")
    texts = [text for text, literal in _sent_commands(run) if literal]
    assert texts == [_DEFINE, _wipe("/tmp/it's/a.py")]
    raw = [call.args[0][6] for call in run.call_args_list if "-l" in call.args[0]]
    assert raw and not any("it's" in text or "it''s" in text for text in raw)


def test_show_fresh_in_new_tab_opens_tab_before_renaming(
    sent: list[str], follower: TmuxVimFollower
) -> None:
    follower.show_fresh("/tmp/new.py", "line1\n", in_new_tab=True)
    wipe = sent.index("text::" + _wipe("/tmp/new.py"))
    # The tab opens on the rename's guarded line, only once the path read
    # succeeded, never as a line of its own (a stray tab on a failed read).
    rename = sent.index("text::" + _rename("/tmp/new.py", in_new_tab=True))
    assert wipe < rename
    assert "text:::tabnew" not in sent


def test_show_fresh_default_renames_in_place(sent: list[str], follower: TmuxVimFollower) -> None:
    follower.show_fresh("/tmp/new.py", "line1\n")
    assert "text::" + _rename("/tmp/new.py") in sent
    assert not any("tabnew" in entry for entry in sent)


_SWAP_BACK_ON = call_spelled("swap_back_on")


def test_show_fresh_without_a_window_never_turns_swap_back_on() -> None:
    # No window id means no FollowerState to read, so the follower cannot
    # prove it is driving the user's own editor: it keeps the dedicated
    # behaviour (swap stays off), even when some state for the pane says
    # adopted.
    _register_fake_follower("@1", "%2")
    state.FollowerState.update("@1", adopted=True)
    follower = TmuxVimFollower(pane_id="%2")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        follower.show_fresh("/tmp/f.txt", "a\n")
    assert (_SWAP_BACK_ON, True) not in _sent_commands(run)


@pytest.mark.parametrize("adopted", [True, False], ids=["adopted", "dedicated"])
def test_show_fresh_turns_swap_back_on_after_the_rename_only_when_adopted(
    adopted: bool,
) -> None:
    # The rename runs with swap off (a live Vim holding `.swp` made `:file`
    # raise E325). An adopted Vim is the user's own editor, so its buffer
    # gets swap back on right after, with ATTENTION suppressed for that one
    # step; a dedicated follower's stays off.
    _register_fake_follower("@1", "%2")
    state.FollowerState.update("@1", adopted=adopted)
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    # (conftest's autouse cache isolation holds the state the follower reads)
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        follower.show_fresh("/tmp/f.txt", "a\n")
    commands = _sent_commands(run)
    rename = commands.index((_rename("/tmp/f.txt", adopted=adopted), True))
    if adopted:
        assert commands[rename + 1 : rename + 8] == [
            ("Enter", False),
            ("Escape", False),
            ("Escape", False),
            (landed_line(), True),
            ("Enter", False),
            (_SWAP_BACK_ON, True),
            ("Enter", False),
        ]
    else:
        assert (_SWAP_BACK_ON, True) not in commands


# ---- adopted Vim: the user's readonly (see tmux_vim's s:note_user_readonly)


def _adopted_follower() -> TmuxVimFollower:
    _register_fake_follower("@1", "%2")
    state.FollowerState.update("@1", adopted=True)
    return TmuxVimFollower(pane_id="%2", window_id="@1")


@pytest.mark.parametrize("outcome", ["completed", "interrupted"])
def test_an_adopted_animation_notes_claims_and_restores_the_readonly(outcome: str) -> None:
    follower = _adopted_follower()
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch(
            "vim_ai_follower.backends.tmux_vim.run_ops",
            return_value=AnimationResult(outcome, 0),  # type: ignore[arg-type]
        ),
    ):
        follower.apply_edit("/tmp/f.txt", compute_edit_script("a\n", "b\n"))
    commands = _sent_commands(run)
    assert (_ADOPTED_UNLOCK, True) in commands
    tail = [(_restore(), True), ("Enter", False)]
    if outcome == "completed":
        assert commands[-4:] == [(_RELOCK_SYNCED, True), ("Enter", False), *tail]
    else:
        assert (_RELOCK_SYNCED, True) not in commands
        assert commands[-2:] == tail


def test_an_adopted_show_fresh_relock_claims_its_own_readonly() -> None:
    follower = _adopted_follower()
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        follower.show_fresh("/tmp/f.txt", "a\n")
    commands = _sent_commands(run)
    assert commands[-4:] == [
        (call_spelled("relock", 1, 1), True),
        ("Enter", False),
        (_restore(), True),
        ("Enter", False),
    ]


@pytest.mark.parametrize(("entry", "discard"), [("ensure_showing", 0), ("reload_and_relock", 1)])
def test_an_adopted_reload_notes_the_users_readonly_before_it_and_claims_the_lock(
    entry: str, discard: int
) -> None:
    follower = _adopted_follower()
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        getattr(follower, entry)("/tmp/f.txt")
    assert _sent_commands(run)[-4:] == [
        (call_spelled("reload", discard, 1), True),
        ("Enter", False),
        (_ADOPTED_LOCK_READONLY, True),
        ("Enter", False),
    ]


def test_an_adopted_hand_over_restores_the_readonly() -> None:
    follower = _adopted_follower()
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.hand_over()
    assert _sent_commands(run)[-4:] == [
        (_DEFINE, True),
        ("Enter", False),
        (_restore(), True),
        ("Enter", False),
    ]


def test_a_dedicated_follower_never_asks_whose_readonly_it_is() -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        assert follower.user_readonly("/tmp/f.txt") is False
    run.assert_not_called()


@pytest.mark.parametrize(("answer", "expected"), [("1\n", True), ("0\n", False)])
def test_an_adopted_vim_answers_from_the_restore_it_just_ran(answer: str, expected: bool) -> None:
    follower = _adopted_follower()
    (cache.CACHE_DIR / "readonly-2.txt").write_text(answer)
    assert follower.user_readonly("/tmp/f.txt") is expected


def test_no_answer_from_the_restore_is_the_plain_cue(caplog: pytest.LogCaptureFixture) -> None:
    follower = _adopted_follower()
    with (
        patch("vim_ai_follower.backends.tmux_vim._PROBE_TIMEOUT_SECONDS", 0.05),
        patch("vim_ai_follower.backends.tmux_vim.time.sleep"),
    ):
        assert follower.user_readonly("/tmp/f.txt") is False
    assert "no readonly answer" in caplog.text


def test_the_restore_clears_the_last_answer_before_it_is_sent() -> None:
    """A stale answer from an earlier hand-off must never be read as this one's."""
    follower = _adopted_follower()
    answer = cache.CACHE_DIR / "readonly-2.txt"
    answer.parent.mkdir(parents=True, exist_ok=True)
    answer.write_text("1\n")
    with patch("vim_ai_follower.tmux.subprocess.run"):
        follower.hand_over()
    assert not answer.exists()


def test_case_folds_asks_the_filesystem_and_is_zero_whenever_it_cannot(tmp_path: Path) -> None:
    """{folds} for the lookups: whether the path with its letters' case
    swapped is the same file. Never 'fileignorecase' (see
    tests/test_integration_same_file.py for two files differing only in case
    on a case-sensitive volume)."""
    existing = tmp_path / "Readme.md"
    existing.write_text("x\n")
    insensitive = Path(str(existing).swapcase()).exists()
    assert _case_folds(str(existing)) == int(insensitive)
    # No letter to swap, a missing file, a path no filesystem call takes.
    assert _case_folds("/0/1.2") == 0
    assert _case_folds(str(tmp_path / "missing.md")) == 0
    assert _case_folds("/tmp/a\0b") == 0
