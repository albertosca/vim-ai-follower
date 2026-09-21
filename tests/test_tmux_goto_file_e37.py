"""Investigation into goto_file's `:tab drop {file}` hitting a real E37 on a
dirty target buffer (see the task report at
scratchpad/backlog/M-tmux-e37-report.md for the full write-up). Two things
this file exists to pin down, both at the Python/keystroke level (the real-
editor behavior is covered by tests/test_integration_goto_file_e37.py):

1. goto_file sends the exact same `:tab drop {file}` Ex line every time,
   with no Python-side branching on the target's dirty/tab state. This is
   architectural, not a missed opportunity: TmuxPane (src/vim_ai_follower/
   tmux.py) is write-only over tmux send-keys — there is no capture-pane,
   no `:redir`, nothing that reads the pane back — so goto_file has no way
   to ask Vim "is this buffer already current, or dirty, or shown
   elsewhere" before deciding what to send. Any guard for the E37 case
   would have to be a Vim-side conditional embedded IN that Ex line
   (`:if ... | tab drop ... | endif`), which changes the literal string
   for every call, not just the dirty one.

2. That same literal string is pinned byte-for-byte, unconditionally, by
   many tests outside this task's scope: test_tmux_vim.py (goto_file itself
   plus ensure_showing/reload_and_relock/apply_edit/resume/close_tab, each
   asserting exact keystroke sequences) and test_cli_hooks.py (which pins
   `:tab drop {target}` through hooks.py's own navigation calls — the file
   the OTHER agent in this sweep owns). Changing goto_file's Ex line, for
   ANY reason, breaks both. This file documents the measured invariant the
   current (unchanged) design relies on instead of adding a guard: every
   caller's next literal command after goto_file's `:tab drop`/`Enter` pair
   is itself colon-prefixed. That property is what makes the transient E37
   (see the integration twin) harmless in practice today — it is NOT
   required by Vim (measured: there is no actual hit-enter block to
   dismiss, see the integration file), but it is worth locking in as
   documentation of the current design, since callers were written assuming
   the pre-2026 "self-healing via a colon-prefixed dismiss" theory.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from vim_ai_follower import control
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import EditOp, compute_edit_script


def _sent_commands(run_mock: MagicMock) -> list[tuple[str, bool]]:
    """(text, literal) pairs, in order, for send-keys calls targeting %2.
    Duplicated from test_tmux_vim.py's own helper rather than imported: new
    tests in this sweep stay in new files (see the task's GLOBAL contract),
    and importing across sibling test modules would create exactly the kind
    of cross-file coupling that contract is trying to avoid."""
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


def _assert_every_tab_drop_is_followed_by_a_colon_command(
    commands: list[tuple[str, bool]], file_path: str
) -> None:
    """The invariant every caller currently relies on to make a transient
    E37 harmless: whatever comes right after `:tab drop {file}` + Enter is
    itself an Ex command (leading ':'), never a plain keystroke a hit-enter
    prompt could swallow."""
    drop = (f":tab drop {file_path}", True)
    drop_indices = [i for i, c in enumerate(commands) if c == drop]
    assert drop_indices, f"no ':tab drop {file_path}' found in {commands!r}"
    for index in drop_indices:
        # index+1 is the Enter that submits the ex command line.
        assert commands[index + 1] == ("Enter", False)
        next_text, next_literal = commands[index + 2]
        assert next_literal is True
        assert next_text.startswith(":"), (
            f"command right after goto_file's tab drop was {next_text!r}, "
            "not colon-prefixed — the harmless-E37 invariant this sweep "
            "measured would no longer hold"
        )


def test_goto_file_always_sends_the_same_tab_drop_line_no_python_branching() -> None:
    """goto_file cannot see whether the target buffer is dirty or already
    current — TmuxPane has no read/capture path — so it sends the identical
    Ex line unconditionally. Two calls, two different (irrelevant to this
    process) target states, same literal output."""
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.goto_file("/tmp/dirty_and_current.py")
        follower.goto_file("/tmp/clean_and_elsewhere.py")
    commands = _sent_commands(run)
    assert commands == [
        ("Escape", False),
        ("Escape", False),
        (":tab drop /tmp/dirty_and_current.py", True),
        ("Enter", False),
        ("Escape", False),
        ("Escape", False),
        (":tab drop /tmp/clean_and_elsewhere.py", True),
        ("Enter", False),
    ]


def test_ensure_showing_colon_dismiss_invariant_holds() -> None:
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.ensure_showing("/tmp/f.py")
    _assert_every_tab_drop_is_followed_by_a_colon_command(_sent_commands(run), "/tmp/f.py")


def test_reload_and_relock_colon_dismiss_invariant_holds() -> None:
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.reload_and_relock("/tmp/f.py")
    _assert_every_tab_drop_is_followed_by_a_colon_command(_sent_commands(run), "/tmp/f.py")


def test_close_tab_colon_dismiss_invariant_holds() -> None:
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.close_tab("/tmp/f.py")
    _assert_every_tab_drop_is_followed_by_a_colon_command(_sent_commands(run), "/tmp/f.py")


def test_apply_edit_colon_dismiss_invariant_holds_including_on_resume(tmp_path: Path) -> None:
    # Same pause/resume shape as test_tmux_vim.py's
    # test_apply_edit_renavigates_to_its_own_tab_on_resume: goto_file fires
    # twice (initial preamble + mid-resume re-navigation), both must hold.
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
        follower.apply_edit("/tmp/f.py", compute_edit_script("a\n", "b\n"))
    _assert_every_tab_drop_is_followed_by_a_colon_command(_sent_commands(run), "/tmp/f.py")


def test_resume_colon_dismiss_invariant_holds() -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    pending = control.PendingApplyEdit(ops=[op], pace_seconds=0.0, file_path="/tmp/f.py")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        follower.resume(pending)
    _assert_every_tab_drop_is_followed_by_a_colon_command(_sent_commands(run), "/tmp/f.py")
