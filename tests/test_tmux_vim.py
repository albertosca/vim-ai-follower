from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import cache, config, control, state
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends import get_follower
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import EditOp, compute_edit_script


def _goto(path: str) -> str:
    """The exact Ex line goto_file sends to navigate to `path`.

    Spelled out here rather than imported from tmux_vim._GOTO_FILE on
    purpose: these assertions exist to catch an unintended change to that
    constant, and importing it would make every one of them agree with
    whatever the constant happens to say. The `:try`/`:catch` wrapper
    swallows E37 (a modified target buffer) and nothing else; the
    `SwapExists` hook around it answers the swap-file ATTENTION dialog
    with `(E)dit anyway` and is torn down in `finally` — see _GOTO_FILE's
    comment for why the bang, `:silent!`, 'hidden', 'shortmess' and
    'noswapfile' were all measured and rejected."""
    return (
        ":let g:vaf_p = '" + path.replace("'", "''") + "'"
        " | let g:vaf_n = get(filter(range(1, bufnr('$')), 'bufexists(v:val)"
        " && fnamemodify(bufname(v:val), '':p'') ==# fnamemodify(g:vaf_p, '':p'')'), 0, -1)"
        ' | exe "augroup vim_ai_follower_swap"'
        " | exe \"autocmd SwapExists * ++once let v:swapchoice = 'e'\""
        ' | exe "augroup END"'
        " | try"
        " | if g:vaf_n < 0 | exe 'tab drop ' . fnameescape((fnamemodify(fnamemodify(g:vaf_p, ':.'),"
        " ':p') ==# fnamemodify(g:vaf_p, ':p') ? fnamemodify(g:vaf_p, ':.') : g:vaf_p))"
        " | elseif g:vaf_n != bufnr('%')"
        " | exe win_gotoid(get(win_findbuf(g:vaf_n), 0)) ? '' : 'tab sbuffer ' . g:vaf_n"
        " | endif"
        r" | catch /^Vim\%((\a\+)\)\=:E37:/"
        ' | finally | exe "autocmd! vim_ai_follower_swap"'
        ' | exe "augroup! vim_ai_follower_swap"'
        " | unlet! g:vaf_p g:vaf_n | endtry"
    )


# ensure_showing's clean-only disk re-read, spelled out for the same reason
# as _goto: importing tmux_vim._RELOAD_IF_CLEAN would agree with any change.
_RELOAD_IF_CLEAN = (
    ':exe "augroup vim_ai_follower_swap"'
    " | exe \"autocmd SwapExists * ++once let v:swapchoice = 'e'\""
    ' | exe "augroup END"'
    " | try | if !&modified | silent edit | endif"
    ' | finally | exe "autocmd! vim_ai_follower_swap"'
    ' | exe "augroup! vim_ai_follower_swap" | endtry'
)


# The completion relocks' `:e!` with the scoped (E)dit-anyway answer, and the
# des-interrupt/grounding reload, spelled out for the same reason as _goto.
_SYNC_FROM_DISK = (
    ':exe "augroup vim_ai_follower_swap"'
    " | exe \"autocmd SwapExists * ++once let v:swapchoice = 'e'\""
    ' | exe "augroup END"'
    " | try | silent! edit!"
    ' | finally | exe "autocmd! vim_ai_follower_swap"'
    ' | exe "augroup! vim_ai_follower_swap" | endtry'
)
_RELOCK_SYNCED = _SYNC_FROM_DISK + " | setlocal nomodifiable nopaste"
_RELOCK_READONLY_SYNCED = _SYNC_FROM_DISK + " | setlocal readonly nomodifiable nopaste"
# The animation unlock, spelled out for the same reason as _goto.
_UNLOCK_FOR_ANIMATION = ":setlocal noreadonly modifiable paste"
# An ADOPTED Vim's readonly bookkeeping (tmux_vim._NOTE_USER_READONLY), spelled
# out for the same reason as _goto: the note of the user's readonly, the
# follower's claim on the option, and the restore every animation exit sends.
_NOTE = "if !get(b:, 'vaf_ro_ours') | let b:vaf_user_ro = &readonly | endif"
_ADOPTED_UNLOCK = f":{_NOTE} | setlocal noreadonly modifiable paste | let b:vaf_ro_ours = 2"
_ADOPTED_LOCK_READONLY = ":setlocal readonly nomodifiable | let b:vaf_ro_ours = 1"


def _restore() -> str:
    """The restore line, with the answer file user_readonly reads (under the
    test's isolated cache dir, for pane %2)."""
    answer = str(cache.CACHE_DIR / "readonly-2.txt").replace("'", "''")
    return (
        ":if get(b:, 'vaf_user_ro') | setlocal readonly | let b:vaf_ro_ours = 0"
        " | elseif get(b:, 'vaf_ro_ours') == 2 | let b:vaf_ro_ours = 0 | endif"
        " | unlet! b:vaf_user_ro"
        " | try | let g:vaf_r = writefile([&readonly && !get(b:, 'vaf_ro_ours') ? '1' : '0'],"
        f" '{answer}') | catch | finally | unlet! g:vaf_r | endtry"
    )


_RELOAD_DISCARDING = (
    ':exe "augroup vim_ai_follower_swap"'
    " | exe \"autocmd SwapExists * ++once let v:swapchoice = 'e'\""
    ' | exe "augroup END"'
    " | try | silent edit!"
    ' | finally | exe "autocmd! vim_ai_follower_swap"'
    ' | exe "augroup! vim_ai_follower_swap" | endtry'
)


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
    assert commands[2] == (_goto("/tmp/f.txt"), True)
    assert commands[3] == ("Enter", False)
    assert commands[4] == (":silent! CocDisable", True)
    assert commands[5] == ("Enter", False)
    assert commands[6] == (_UNLOCK_FOR_ANIMATION, True)
    assert commands[7] == ("Enter", False)
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
    # no disk sync of any shape once the animation has started (the relock
    # carries it as `silent! edit!`); the rename line's read-then-clear runs
    # before the unlock, on a buffer nothing has been typed into yet
    started = commands.index((_UNLOCK_FOR_ANIMATION, True))
    assert not any("edit!" in text or "e!" in text for text, _ in commands[started:])


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
    assert commands[2] == (_wipe("'/tmp/f.txt'"), True)
    assert commands[3] == ("Enter", False)
    # swap opted out BEFORE the rename: renaming a swap-enabled buffer runs
    # the swap check, and a live Vim holding the file's swap made `:file`
    # raise E325 (see tests/test_e2e_battery_tranche2.py)
    assert commands[4] == (":setlocal noswapfile", True)
    assert commands[5] == ("Enter", False)
    # the rename line also resets buftype (a plugin scratch screen's
    # buftype=nofile, inherited, made the user's :w fail with E382) BEFORE
    # its read, which only reads a file into a regular buffer
    assert commands[6] == (_rename("/tmp/f.txt"), True)
    assert commands[7] == ("Enter", False)
    assert commands[8] == (":filetype detect", True)
    assert commands[9] == ("Enter", False)
    assert commands[10] == (":silent! CocDisable", True)
    assert commands[11] == ("Enter", False)
    assert commands[12] == (_UNLOCK_FOR_ANIMATION, True)
    assert commands[13] == ("Enter", False)
    assert commands[14] == (":%d", True)
    assert commands[15] == ("Enter", False)
    assert commands[16] == ("i", True)
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
        (_wipe("'/tmp/f.txt'"), True),
        ("Enter", False),
        (":setlocal noswapfile", True),
        ("Enter", False),
        (_rename("/tmp/f.txt"), True),
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
    assert commands[0] == (":silent! CocDisable", True)
    assert commands[1] == ("Enter", False)
    assert commands[2] == (_UNLOCK_FOR_ANIMATION, True)
    assert commands[3] == ("Enter", False)
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
    assert commands[0] == (":silent! CocDisable", True)
    assert commands[2] == (_UNLOCK_FOR_ANIMATION, True)
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
    assert not any("e!" in text for text, _ in commands)
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
    assert commands[:4] == [
        ("Escape", False),
        ("Escape", False),
        (_goto("/tmp/f.py"), True),
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
    # Matches on the substring, not a ':tab drop' prefix: goto_file's Ex
    # line now opens with ':try |', so a prefix check would pass whether or
    # not the navigation was skipped.
    assert not any("tab drop" in text for text, _ in commands)


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
        (_goto("/tmp/a.py"), True),
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
        (_goto("/tmp/a.py"), True),
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
        (_goto("/tmp/a.py"), True),
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
        (_goto("/tmp/a.py"), True),
        ("Enter", False),
        (
            ':exe "augroup vim_ai_follower_swap"'
            " | exe \"autocmd SwapExists * ++once let v:swapchoice = 'e'\""
            ' | exe "augroup END"'
            " | try | silent edit!"
            ' | finally | exe "autocmd! vim_ai_follower_swap"'
            ' | exe "augroup! vim_ai_follower_swap" | endtry',
            True,
        ),
        ("Enter", False),
        (":setlocal readonly nomodifiable", True),
        ("Enter", False),
    ]


def _wipe(quoted_path: str) -> str:
    """The exact Ex line an eviction sends to wipe `quoted_path`'s buffer.

    Spelled out rather than imported from tmux_vim._WIPE_BUFFER, for the
    same reason as _goto above: importing would make these assertions agree
    with whatever the constant happens to say. `quoted_path` is already a
    Vim single-quoted string literal — that is the whole point of the line,
    so it is what the caller passes."""
    return (
        f":let g:vaf_wipe_name = fnamemodify({quoted_path}, ':p')"
        " | let g:vaf_wipe_nr = get(filter(range(1, bufnr('$')),"
        ' \'bufexists(v:val) && bufname(v:val) !=# ""'
        ' && fnamemodify(bufname(v:val), ":p") ==# g:vaf_wipe_name\'), 0, -1)'
        " | if g:vaf_wipe_nr > 0 | exe 'silent! bwipeout! ' . g:vaf_wipe_nr | endif"
        " | unlet! g:vaf_wipe_name g:vaf_wipe_nr"
    )


def _rename(path: str, *, adopted: bool = False) -> str:
    """show_fresh's `:file {path}` rename-in-place line, escaped: `#`, `%`
    and a space are live on Vim's command line, so the raw path is wrong —
    it goes through a Vim string literal and fnameescape(), same as _goto,
    and names the buffer relative to Vim's cwd (tmux_vim._vim_display_name).
    Spelled out for the same reason as _goto/_wipe above. An adopted Vim's
    also claims the readonly option for the follower."""
    claim = " | let b:vaf_user_ro = 0 | let b:vaf_ro_ours = 2" if adopted else ""
    literal = "'" + path.replace("'", "''") + "'"
    short = f"fnamemodify({literal}, ':.')"
    return (
        f":exe 'file ' . fnameescape((fnamemodify({short}, ':p') ==# fnamemodify({literal}, ':p')"
        f" ? {short} : {literal}))"
        + claim
        + " | setlocal buftype= modifiable noreadonly"
        + " | noautocmd silent! edit! | silent! %d _"
    )


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
        (_wipe("'/tmp/old.py'"), True),
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
    assert texts == [_wipe(f"'{path}'")]
    assert not any(f"bwipeout! {path}" in text for text in texts)


def test_close_tab_quotes_an_apostrophe_in_the_path() -> None:
    """A Vim single-quoted literal escapes `'` by doubling it, and that is
    the only escape it has. Get this wrong and the literal terminates early,
    turning the rest of the path into broken Vim script."""
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.close_tab("/tmp/it's/a.py")
    texts = [text for text, literal in _sent_commands(run) if literal]
    assert texts == [_wipe("'/tmp/it''s/a.py'")]


def test_show_fresh_in_new_tab_opens_tab_before_renaming(
    sent: list[str], follower: TmuxVimFollower
) -> None:
    follower.show_fresh("/tmp/new.py", "line1\n", in_new_tab=True)
    wipe = sent.index("text::" + _wipe("'/tmp/new.py'"))
    tabnew = sent.index("text:::tabnew")
    rename = sent.index("text::" + _rename("/tmp/new.py"))
    assert wipe < tabnew < rename


def test_show_fresh_default_renames_in_place(sent: list[str], follower: TmuxVimFollower) -> None:
    follower.show_fresh("/tmp/new.py", "line1\n")
    # Vim commands carry their own leading ":", so the recorded entry has
    # three colons — "text::tabnew" would never match anything.
    assert "text:::tabnew" not in sent


_SWAP_BACK_ON = (
    # Spelled out, not imported, for the same reason as _goto/_wipe above.
    ":let g:vaf_shortmess = &shortmess | set shortmess+=A"
    " | try | silent! setlocal swapfile"
    " | finally | let &shortmess = g:vaf_shortmess | unlet g:vaf_shortmess | endtry"
    " | redraw"
)


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
        assert commands[rename + 1 : rename + 4] == [
            ("Enter", False),
            (_SWAP_BACK_ON, True),
            ("Enter", False),
        ]
    else:
        assert (_SWAP_BACK_ON, True) not in commands


# ---- adopted Vim: the user's readonly (see tmux_vim._NOTE_USER_READONLY)


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
        (_RELOCK_READONLY_SYNCED + " | let b:vaf_ro_ours = 1", True),
        ("Enter", False),
        (_restore(), True),
        ("Enter", False),
    ]


@pytest.mark.parametrize(
    ("entry", "reload"),
    [("ensure_showing", _RELOAD_IF_CLEAN), ("reload_and_relock", _RELOAD_DISCARDING)],
)
def test_an_adopted_reload_notes_the_users_readonly_before_it_and_claims_the_lock(
    entry: str, reload: str
) -> None:
    follower = _adopted_follower()
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        getattr(follower, entry)("/tmp/f.txt")
    assert _sent_commands(run)[-4:] == [
        (f":{_NOTE} | {reload[1:]}", True),
        ("Enter", False),
        (_ADOPTED_LOCK_READONLY, True),
        ("Enter", False),
    ]


def test_an_adopted_hand_over_restores_the_readonly() -> None:
    follower = _adopted_follower()
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.hand_over()
    assert _sent_commands(run)[-2:] == [(_restore(), True), ("Enter", False)]


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
