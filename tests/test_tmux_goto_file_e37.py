"""goto_file's E37 guard, at the Python/keystroke level. The real-editor
half — that no hit-enter prompt actually appears at the follower pane's
real 49-column geometry — lives in tests/test_integration_goto_file_e37.py.

What goto_file sends wraps `tab drop {file}` in `:try`/`:catch /<E37>/`
rather than sending a bare `:tab drop {file}`. Why:

`:tab drop` finishes by running `:rewind` over the arglist it just set, and
`:rewind` runs Vim's abandon check against the buffer it has ALREADY landed
on. When that buffer is modified — the ordinary state after an interrupt
hand-off or a killed hook, see _with_unlocked's "a pause never relocks" —
the check raises `E37: No write since last change (add ! to override)`.
That message is 51 characters and the follower pane is 49 (a
`split-window -h` inside a 100-column window), so it wraps, and a wrapped
message is what makes Vim escalate to a blocking "Press ENTER or type
command to continue" prompt in the user's pane.

The navigation itself has already succeeded at that point (measured: right
tab focused, unsaved content untouched, tab count unchanged) — only the
trailing bookkeeping aborts, and nothing in this backend needs it. So the
guard swallows that one error and nothing else. The three alternatives were
each measured against a real tmux+vim and rejected:

  * `:tab drop!` — the bang reloads from disk and silently DISCARDS the
    unsaved content.
  * `:silent! tab drop` — also hides the swap-file "ATTENTION" dialog,
    which blocks Vim just as it did then but now behind a blank screen:
    a visible stall traded for an invisible one. (That dialog is no longer
    merely visible — it is answered, by the `SwapExists` hook the same
    line now carries. `:silent!` stays rejected: it would still hide any
    OTHER prompt the drop raises, and it is not what answers this one.)
  * `:let h=&hidden | set hidden | tab drop | let &hidden=h` — does not
    even work (the same-file path sets Vim's CCGD_MULTWIN flag, which skips
    the 'hidden' escape, so E37 still fires), and the `|`-chained restore
    never runs after the error, leaking 'hidden' ON into what, in adopt
    mode, is the user's own Vim.

Python does no branching here and cannot: TmuxPane is write-only over tmux
send-keys (no capture-pane, no `:redir`, nothing reads the pane back), so
goto_file has no way to ask Vim whether the target is dirty. The guard is
therefore Vim-side and unconditional — the same literal line for every
call, whatever the target's state.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from vim_ai_follower import control
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import EditOp, compute_edit_script


def _goto(path: str) -> str:
    """The exact Ex line goto_file sends. Spelled out rather than imported
    from tmux_vim._GOTO_FILE on purpose: importing the constant would make
    every assertion below agree with whatever it happens to say, which is
    precisely the change these tests exist to catch.

    The `SwapExists` hook wrapping the try/catch is the swap-file half of
    the same guard (tests/test_tmux_swap_choice.py); it is part of the
    literal, so it belongs in this spelling too."""
    return (
        ':exe "augroup vim_ai_follower_swap"'
        " | exe \"autocmd SwapExists * ++once let v:swapchoice = 'e'\""
        ' | exe "augroup END"'
        rf" | try | tab drop {path} | catch /^Vim\%((\a\+)\)\=:E37:/"
        ' | finally | exe "autocmd! vim_ai_follower_swap"'
        ' | exe "augroup! vim_ai_follower_swap" | endtry'
    )


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


def _assert_navigates_through_the_guard(commands: list[tuple[str, bool]], file_path: str) -> None:
    """Every goto_file call site must navigate through the guarded line —
    never a bare `:tab drop`, which is what would put the prompt back."""
    guarded = (_goto(file_path), True)
    indices = [i for i, c in enumerate(commands) if c == guarded]
    assert indices, f"no guarded navigation to {file_path!r} found in {commands!r}"
    for index in indices:
        assert commands[index + 1] == ("Enter", False)
    bare = f":tab drop {file_path}"
    assert not any(text == bare for text, _ in commands), (
        f"an unguarded {bare!r} was sent — a modified target would raise E37 "
        "and leave a blocking hit-enter prompt in the pane"
    )


def test_goto_file_sends_the_guarded_line_with_no_python_branching() -> None:
    """goto_file cannot see whether the target buffer is dirty or already
    current — TmuxPane has no read/capture path — so it sends the identical
    guarded Ex line unconditionally, for every target state."""
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.goto_file("/tmp/dirty_and_current.py")
        follower.goto_file("/tmp/clean_and_elsewhere.py")
    assert _sent_commands(run) == [
        ("Escape", False),
        ("Escape", False),
        (_goto("/tmp/dirty_and_current.py"), True),
        ("Enter", False),
        ("Escape", False),
        ("Escape", False),
        (_goto("/tmp/clean_and_elsewhere.py"), True),
        ("Enter", False),
    ]


def test_the_guard_catches_e37_only_and_keeps_the_path_unescaped() -> None:
    """Two properties of the literal that the call-site tests above would
    not notice on their own.

    The catch pattern is Vim's documented `Vim(cmd):E37:` exception form,
    anchored — a looser one could swallow an unrelated failure. And the
    path stays in `tab drop`'s own file argument, never inside a Vim string
    literal, so the wrapper adds no new quoting rules: whatever `:tab drop`
    already did with spaces, `%`, `#` or wildcards, it still does."""
    line = _goto("/tmp/a b#c%d.py")
    assert " | try | tab drop /tmp/a b#c%d.py | catch /" in line
    assert line.endswith(' | exe "augroup! vim_ai_follower_swap" | endtry')
    assert r"^Vim\%((\a\+)\)\=:E37:" in line
    # The swap hook brought Vim string literals into the line, but NOT
    # around the path: its only occurrence is still the raw one inside
    # `tab drop`, so no escaping rule changed for it.
    assert line.count("/tmp/a b#c%d.py") == 1
    quoted_segments = line.split('"')[1::2]
    assert quoted_segments, "expected the swap hook's exe-quoted segments"
    assert not any("/tmp/a b#c%d.py" in segment for segment in quoted_segments)


def test_ensure_showing_navigates_through_the_guard() -> None:
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.ensure_showing("/tmp/f.py")
    _assert_navigates_through_the_guard(_sent_commands(run), "/tmp/f.py")


def test_reload_and_relock_navigates_through_the_guard() -> None:
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.reload_and_relock("/tmp/f.py")
    _assert_navigates_through_the_guard(_sent_commands(run), "/tmp/f.py")


def test_close_tab_navigates_through_the_guard() -> None:
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.close_tab("/tmp/f.py")
    _assert_navigates_through_the_guard(_sent_commands(run), "/tmp/f.py")


def test_rewrite_buffer_navigates_through_the_guard(tmp_path: Path) -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        follower.rewrite_buffer("/tmp/f.py", "a\n")
    _assert_navigates_through_the_guard(_sent_commands(run), "/tmp/f.py")


def test_apply_edit_navigates_through_the_guard_including_on_resume(
    tmp_path: Path,
) -> None:
    # Same pause/resume shape as test_tmux_vim.py's
    # test_apply_edit_renavigates_to_its_own_tab_on_resume: goto_file fires
    # twice (initial preamble + mid-resume re-navigation), both guarded.
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
    commands = _sent_commands(run)
    _assert_navigates_through_the_guard(commands, "/tmp/f.py")
    assert len([c for c in commands if c == (_goto("/tmp/f.py"), True)]) == 2


def test_resume_navigates_through_the_guard() -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))
    pending = control.PendingApplyEdit(ops=[op], pace_seconds=0.0, file_path="/tmp/f.py")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        follower.resume(pending)
    _assert_navigates_through_the_guard(_sent_commands(run), "/tmp/f.py")
