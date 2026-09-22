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
at all. What this file pins is the exact shape of that one Ex line,
because every property below was a measured failure of some other
shape:

  * The hook is registered in its own augroup, and the augroup is opened
    by an `augroup` command FIRST. `:autocmd {group} ...` does not create
    a missing group — it fails with `E216` and leaves its own hit-enter
    prompt, which is the very failure being fixed.
  * There is no bare `:autocmd!` in the line. It would only ever apply to
    the follower's own group, but if the preceding `augroup` ever failed
    it would run in the DEFAULT group and wipe every autocommand the user
    has.
  * Teardown is inside `finally`, not after `endtry`, so it runs on the
    E37 path and on an uncaught error too.
  * `++once` bounds the one residual risk — the line being cut off
    mid-flight, before `finally` — to a single auto-answered dialog
    rather than the policy persisting in the user's Vim.
  * Scoping is by TIME, not by pattern: the hook exists only for the
    duration of the drop, so a user `:e` of a swapped file afterwards
    still gets the normal dialog. Matching on the path instead would mean
    escaping it into an autocmd pattern, and the path deliberately never
    enters a Vim string literal.

`shortmess+=A` and `set noswapfile` also clear the dialog on screen, and
both were measured and rejected: they are global, are never restored,
and silently disarm the user's own swap protection from then on. Neither
appears in the line, and a test below says so.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from vim_ai_follower.backends.tmux_vim import TmuxVimFollower

_GROUP = "vim_ai_follower_swap"


def _sent_text(run_mock: MagicMock) -> list[str]:
    """Only the literal `send-keys -l` payloads, in order."""
    return [
        call.args[0][-1] for call in run_mock.call_args_list if call.args and "-l" in call.args[0]
    ]


def _goto_line(path: str = "/tmp/f.py") -> str:
    follower = TmuxVimFollower(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        follower.goto_file(path)
    sent = _sent_text(run)
    assert len(sent) == 1, f"goto_file sent {len(sent)} literal payloads: {sent!r}"
    return sent[0]


def test_the_line_registers_the_swap_hook_before_the_drop() -> None:
    """Order is the whole point: the hook has to exist by the time
    `tab drop` opens the file, or the dialog is already on screen."""
    line = _goto_line()
    register = line.index("SwapExists")
    drop = line.index("tab drop")
    assert register < drop, f"the hook is registered after the drop:\n{line}"


def test_the_hook_answers_edit_anyway_and_nothing_else() -> None:
    """'e' is Edit anyway. The choices this must NOT make are 'r'
    (Recover, which would show the other Vim's swap contents instead of
    the file), 'q'/'a' (abort, which stalls the navigation) and 'd'
    (Delete it, which destroys someone's recovery data)."""
    line = _goto_line()
    assert "let v:swapchoice = 'e'" in line
    for rejected in ("'r'", "'q'", "'a'", "'d'", "'o'"):
        assert f"v:swapchoice = {rejected}" not in line


def test_the_hook_is_registered_in_its_own_augroup_opened_first() -> None:
    """`:autocmd {group} ...` does NOT create a missing group: it fails
    with E216 and leaves its own hit-enter prompt. So the line must open
    the group with an `augroup` command before defining the autocommand,
    and close it again."""
    line = _goto_line()
    open_group = line.index(f'exe "augroup {_GROUP}"')
    define = line.index("autocmd SwapExists")
    close_group = line.index('exe "augroup END"')
    assert open_group < define < close_group, line
    # The inline-group spelling is the E216 trap; it must not appear.
    assert f"autocmd {_GROUP} SwapExists" not in line


def test_the_line_contains_no_bare_autocmd_bang() -> None:
    """A bare `:autocmd!` runs in whatever group is current. It is only
    safe while the preceding `augroup` is known to have succeeded — and
    if it ever ran in the default group it would delete every
    autocommand the user has. Nothing in the line needs it, because
    `finally` clears the group on every pass."""
    line = _goto_line()
    for command in line.split(" | "):
        stripped = command.removeprefix(":").strip()
        assert stripped != 'exe "autocmd!"', f"bare autocmd! in:\n{line}"
    # Every teardown of autocommands names the group explicitly.
    assert line.count("autocmd!") == line.count(f"autocmd! {_GROUP}") == 1


def test_teardown_is_in_finally_so_it_survives_the_e37_path() -> None:
    """The same line already swallows E37 from `tab drop`'s trailing
    `:rewind`. Teardown placed after `endtry` would be skipped on any
    uncaught error, leaving the swap policy live in what, in adopt mode,
    is the user's own Vim."""
    line = _goto_line()
    finally_at = line.index("| finally |")
    assert finally_at < line.index(f'exe "autocmd! {_GROUP}"')
    assert finally_at < line.index(f'exe "augroup! {_GROUP}"')
    assert line.index("catch /") < finally_at
    assert line.endswith("| endtry")


def test_the_hook_is_marked_once() -> None:
    """Bounds the residual risk: if this line is ever cut off before
    `finally` runs, the stray hook answers at most one dialog and then
    removes itself, instead of persisting as a silent policy change."""
    assert "autocmd SwapExists * ++once let" in _goto_line()


def test_the_line_never_touches_shortmess_or_swapfile() -> None:
    """Both clear the dialog and both leak: they are global, are never
    restored, and from then on the USER's own `:e` of a swapped file
    opens with no warning (shortmess) or with no crash recovery at all
    (noswapfile)."""
    line = _goto_line()
    assert "shortmess" not in line
    assert "swapfile" not in line
    assert "noswapfile" not in line


def test_the_swap_hook_never_carries_the_path() -> None:
    """The hook's `exe "..."` segments are double-quoted Vim strings, where
    a backslash or `"` in a path would be reinterpreted. The path must stay
    out of all of them and appear exactly once, as the single-quoted
    literal handed to fnameescape()."""
    path = "/tmp/a b#c%d'e.py"
    line = _goto_line(path)
    literal = "'/tmp/a b#c%d''e.py'"
    assert line.count(literal) == 1
    quoted = line.split('"')[1::2]
    assert quoted, "expected the exe-quoted segments the hook is built from"
    assert not any("a b#c" in segment for segment in quoted)
    assert f"fnameescape({literal})" in line


def test_every_navigation_carries_the_swap_hook() -> None:
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
        drops = [text for text in _sent_text(run) if "tab drop" in text]
        assert drops, f"{name} sent no navigation at all"
        for drop in drops:
            assert "SwapExists" in drop, f"{name} navigates without the hook: {drop!r}"
