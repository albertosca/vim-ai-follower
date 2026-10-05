"""goto_file's swap-file guard, at the Python/keystroke level. The
real-editor half — that no ATTENTION dialog actually appears at the
follower pane's real 49-column geometry, and that the other Vim keeps
working — lives in tests/test_integration_swap_choice.py.

When the target has a `.file.swp`, `:tab drop` raises Vim's
`E325: ATTENTION ... Swap file "..." already exists!` and waits on
`[O]pen Read-Only, (E)dit anyway, (R)ecover, (Q)uit, (A)bort` (plus
`(D)elete it` when the swap is stale). Two states reach this in normal
use: another Vim holding the file open — the everyday adopt-mode case,
the user's own editor on the file Claude is writing — and a stale swap
left by a crash. At 49 columns the ATTENTION text is long enough to hit
the `-- More --` pager BEFORE the question even appears, so the pane is
stuck twice over and every keystroke the follower sends afterwards
answers a prompt instead of navigating.

Policy (Alberto, 2026-09-21): answer `(E)dit anyway`, adopt mode
included. The follower never writes the file — its buffers are
display-only and relocked read-only — so editing anyway cannot clobber
the other Vim's work, and choosing Edit (never `(D)elete it`) leaves the
swap file itself on disk, so nobody's recovery data is destroyed.

The mechanism is Vim's own `SwapExists` autocommand setting
`v:swapchoice`, which answers the question without typing into a prompt
at all. What this file pins is the exact shape of the Vim functions
that carry it (s:goto, s:swap_answer_open/close, s:reload in
tmux_vim._VIM_SCRIPT), because every property below was a measured
failure of some other shape:

  * The hook is registered in its own augroup, and the augroup is opened
    by an `augroup` command FIRST. `:autocmd {group} ...` does not create
    a missing group — it fails with `E216` and leaves its own hit-enter
    prompt, which is the very failure being fixed.
  * There is no bare `:autocmd!` anywhere. It would only ever apply to
    the follower's own group, but if the preceding `augroup` ever failed
    it would run in the DEFAULT group and wipe every autocommand the user
    has.
  * Teardown is inside `finally`, not after `endtry`, so it runs on the
    E37 path and on an uncaught error too.
  * `++once` bounds the one residual risk — the call being cut off
    mid-flight, before `finally` — to a single auto-answered dialog
    rather than the policy persisting in the user's Vim.
  * Scoping is by TIME, not by pattern: the hook exists only for the
    duration of the drop, so a user `:e` of a swapped file afterwards
    still gets the normal dialog. Matching on the path instead would mean
    escaping it into an autocmd pattern, and the path deliberately never
    enters a Vim string literal: it is read from a handle file.

`shortmess+=A` and `set noswapfile` also clear the dialog on screen, and
both were measured and rejected: they are global, are never restored,
and silently disarm the user's own swap protection from then on. Neither
appears in the navigation, and a test below says so.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from helpers import call_spelled, case_folds, resolve_typed_paths, typed_path, vim_function

from vim_ai_follower.backends.tmux_vim import TmuxVimFollower, _vim_functions

_GROUP = "vim_ai_follower_swap"


def _sent_text(run_mock: MagicMock) -> list[str]:
    """Only the literal `send-keys -l` payloads, in order."""
    return [
        call.args[0][-1] for call in run_mock.call_args_list if call.args and "-l" in call.args[0]
    ]


def test_the_navigation_registers_the_swap_hook_before_the_drop() -> None:
    """Order is the whole point: the hook has to exist by the time
    `tab drop` opens the file, or the dialog is already on screen."""
    goto = vim_function("goto")
    register = goto.index("call s:swap_answer_open()")
    drop = goto.index("tab drop")
    assert register < drop, f"the hook is registered after the drop:\n{goto}"


def test_the_hook_answers_edit_anyway_and_nothing_else() -> None:
    """'e' is Edit anyway. The choices this must NOT make are 'r'
    (Recover, which would show the other Vim's swap contents instead of
    the file), 'q'/'a' (abort, which stalls the navigation) and 'd'
    (Delete it, which destroys someone's recovery data)."""
    script = _vim_functions()[1]
    assert "let v:swapchoice = 'e'" in vim_function("swap_answer_open")
    assert script.count("v:swapchoice") == 1
    for rejected in ("'r'", "'q'", "'a'", "'d'", "'o'"):
        assert f"v:swapchoice = {rejected}" not in script


def test_the_hook_is_registered_in_its_own_augroup_opened_first() -> None:
    """`:autocmd {group} ...` does NOT create a missing group: it fails
    with E216 and leaves its own hit-enter prompt. So the group is opened
    with an `augroup` command before the autocommand is defined, and
    closed again."""
    register = vim_function("swap_answer_open")
    open_group = register.index(f"augroup {_GROUP}\n")
    define = register.index("autocmd SwapExists")
    close_group = register.index("augroup END")
    assert open_group < define < close_group, register
    # The inline-group spelling is the E216 trap; it must not appear.
    assert f"autocmd {_GROUP} SwapExists" not in _vim_functions()[1]


def test_the_script_contains_no_bare_autocmd_bang() -> None:
    """A bare `:autocmd!` runs in whatever group is current. It is only
    safe while the preceding `augroup` is known to have succeeded — and
    if it ever ran in the default group it would delete every
    autocommand the user has. Nothing needs it, because `finally` clears
    the group on every pass."""
    script = _vim_functions()[1]
    assert script.count("autocmd!") == script.count(f"autocmd! {_GROUP}") == 1
    teardown = vim_function("swap_answer_close")
    assert teardown.index(f"autocmd! {_GROUP}") < teardown.index(f"augroup! {_GROUP}")


def test_teardown_is_in_finally_so_it_survives_the_e37_path() -> None:
    """The navigation swallows E37 from `tab drop`'s trailing `:rewind`.
    Teardown placed after `endtry` would be skipped on any uncaught error,
    leaving the swap policy live in what, in adopt mode, is the user's own
    Vim."""
    goto = vim_function("goto")
    finally_at = goto.index("  finally\n")
    assert goto.index("catch /") < finally_at
    assert finally_at < goto.index("call s:swap_answer_close()") < goto.index("endtry")


def test_the_hook_is_marked_once() -> None:
    """Bounds the residual risk: if the call is ever cut off before
    `finally` runs, the stray hook answers at most one dialog and then
    removes itself, instead of persisting as a silent policy change."""
    assert "autocmd SwapExists * ++once let" in vim_function("swap_answer_open")


def test_the_navigation_never_touches_shortmess_or_swapfile() -> None:
    """Both clear the dialog and both leak: they are global, are never
    restored, and from then on the USER's own `:e` of a swapped file
    opens with no warning (shortmess) or with no crash recovery at all
    (noswapfile)."""
    for name in ("goto", "swap_answer_open", "swap_answer_close"):
        body = vim_function(name)
        assert "shortmess" not in body
        assert "swapfile" not in body


def test_the_swap_hook_never_carries_the_path() -> None:
    """The path is not on the typed line at all: the call names a handle
    file, which s:goto reads into `g:vaf_p` and hands to fnameescape()."""
    path = "/tmp/a b#c%d'e.py"
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.goto_file(path)
    sent = _sent_text(run)
    assert not any("a b#c" in line for line in sent)
    resolved = [resolve_typed_paths(line) for line in sent]
    assert call_spelled("goto", typed_path(path), case_folds(path)) in resolved
    goto = vim_function("goto")
    assert "let g:vaf_p = s:read(a:token)" in vim_function("acting_start")
    assert "exe 'silent tab drop ' . fnameescape(s:display(g:vaf_p))" in goto


def test_every_navigation_goes_through_the_guarded_goto() -> None:
    """goto_file is the single navigation preamble, so one guard covers
    every caller. If any method ever grew its own `tab drop`, it would
    reintroduce the stall on exactly the paths this fix is for."""
    follower = TmuxVimFollower(pane_id="%2")
    callers = (
        ("ensure_showing", ("/tmp/f.py",)),
        ("reload_and_relock", ("/tmp/f.py",)),
        ("goto_file", ("/tmp/f.py",)),
    )
    for name, args in callers:
        with patch("vim_ai_follower.tmux.subprocess.run") as run:
            getattr(follower, name)(*args)
        sent = _sent_text(run)
        assert any("('goto'," in text for text in sent), f"{name} sent no navigation"
        assert not any("tab drop" in text for text in sent), sent
    script = _vim_functions()[1]
    assert script.count("tab drop") == 1, "a second `tab drop` outside s:goto"


def test_ensure_showings_reload_carries_the_same_scoped_answer() -> None:
    """ensure_showing re-reads a clean buffer with `:edit`, which re-runs
    the swap-name search and raises ATTENTION on its own when another Vim
    owns `.swp` (measured; tests/test_integration_swap_choice.py). So the
    reload is guarded exactly like the drop: hook registered before the
    `edit`, torn down in `finally`, never `silent!` (which would hide a
    dialog behind a blank screen), and a dirty buffer is skipped."""
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.ensure_showing("/tmp/f.py")
    reloads = [text for text in _sent_text(run) if "'reload'" in text]
    assert [resolve_typed_paths(text) for text in reloads] == [call_spelled("reload", 0, 0)]
    reload = vim_function("reload")
    assert reload.index("call s:swap_answer_open()") < reload.index("silent edit\n")
    assert reload.index("silent edit\n") < reload.index("  finally\n")
    assert reload.index("  finally\n") < reload.index("call s:swap_answer_close()")
    assert "elseif !&modified\n      silent edit\n" in reload
    assert "silent!" not in reload
