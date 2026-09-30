from __future__ import annotations

import contextlib
import logging
import os
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vim_ai_follower import cache, config
from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, AnimationResult, run_lines, run_ops
from vim_ai_follower.backends import BufferProbe, classify_buffer
from vim_ai_follower.control import PendingApplyEdit, PendingShowFresh
from vim_ai_follower.diff import EditOp
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane

# The lock protocol, as literal Vim ex-command lines. The follower buffer is
# read-only by default so a stray keystroke can't corrupt it; an animation
# unlocks around itself and relocks after. Both relocks prepend a silent `:e!`
# disk sync (the buffer's name already matches the file Claude wrote, so the
# reload is visually a no-op but clears W11 staleness). The two relocks differ
# only in whether they re-assert `readonly`: a fresh retype gets the stronger
# read-only lock, an in-place edit is merely made unmodifiable. The one relock
# without the `:e!` is the pre-edit catch-up's (resume with reload=False),
# whose end state is the next edit's base, not the file on disk.
#
# The unlock clears 'readonly' as well. Typing into a readonly buffer raises
# `W10: Warning: Changing a readonly file`, which at the follower pane's 49
# columns becomes a blocking hit-enter prompt that swallows the rest of the
# animation. A buffer is left readonly by show_fresh's relock and by
# ensure_showing's lock, and an Edit that follows either one used to be
# rescued by accident: goto_file's `:tab drop` re-read the file, and a reload
# resets 'readonly'. Now that goto_file never reloads a loaded buffer (see
# _GOTO_FILE), the unlock has to say it (measured 2026-09-23).
#
# But in an ADOPTED Vim a readonly the USER set (`:view`, `vim -R`) is theirs
# to keep, and both the unlock and the completion relock's `:e!` (a reload
# resets 'readonly') took it away, for good on the interrupted path. So an
# adopted Vim's lines record the user's readonly in `b:vaf_user_ro` and
# _RESTORE_READONLY puts it back on every exit: after the relock, at an
# interrupt, and in hand_over. Paused time never leaves the run, so it needs
# nothing. A DEDICATED follower's readonly is never the user's (ruling,
# 2026-09-28): it gets the plain lines above and no restore at all.
#
# Whose readonly it is decides everything: the follower sets 'readonly' itself
# (ensure_showing's lock, show_fresh's relock), and handing THAT back at an
# interrupt turns the cue's ":w releases" into E45 with Claude still blocked.
# The follower therefore marks its own claim on the option in `b:vaf_ro_ours`:
# 1 while its readonly lock is on, 2 while an animation has it unlocked, 0 or
# absent when the value is the user's. A buffer variable survives every reload
# (`:edit`, `:checktime` under 'autoread'), so a reload cannot fake the user's
# readonly: a changedtick stamp could, and did (a formatter's rewrite reloaded
# the follower's readonly buffer, the tick moved, and the follower's readonly
# was handed back as the user's; measured 2026-09-28).
#
# _NOTE_USER_READONLY runs before every step of the follower's that changes
# the option (the Read's reload and lock, the des-interrupt's reload, the
# unlock): while the follower has no claim, the current value is the user's
# and is recorded, 0 included, so a readonly the user dropped since is not
# restored later. While the follower's claim is on, the earlier record stands;
# that is also what carries it across a killed hook or an exception mid-run.
# The restore drops the record once used. The one thing it cannot see is the
# user changing 'readonly' while the follower's own lock is on (a bare
# `:setlocal readonly`, or `:view` of a file the follower has locked): with
# no autocommand in the user's Vim, that is indistinguishable from the
# follower's own value, and the follower's claim wins.
_LOCK_READONLY = ":setlocal readonly nomodifiable"
_UNLOCK_FOR_ANIMATION = ":setlocal noreadonly modifiable paste"
_RELOCK = ":setlocal nomodifiable nopaste"
_RELOCK_READONLY = ":setlocal readonly nomodifiable nopaste"
_NOTE_USER_READONLY = "if !get(b:, 'vaf_ro_ours') | let b:vaf_user_ro = &readonly | endif"
_ADOPTED_LOCK_READONLY = _LOCK_READONLY + " | let b:vaf_ro_ours = 1"
_ADOPTED_UNLOCK_FOR_ANIMATION = (
    f":{_NOTE_USER_READONLY} | {_UNLOCK_FOR_ANIMATION[1:]} | let b:vaf_ro_ours = 2"
)
# The restore also leaves its answer for user_readonly (the hand-off cue) in
# {answer}: whether the buffer is now readonly by the user's setting. Written
# here, by the line the follower sends anyway, so the cue costs no extra
# keystrokes into a Vim the user is about to type into (a separate probe line
# at the hand-off interleaved with the user's first keys; measured
# 2026-09-28 with a test's keys, which garbled both command lines). Inside
# try/catch and via `let`, like _PROBE_BUFFER: no failure may leave a prompt.
_RESTORE_READONLY = (
    ":if get(b:, 'vaf_user_ro') | setlocal readonly | let b:vaf_ro_ours = 0"
    " | elseif get(b:, 'vaf_ro_ours') == 2 | let b:vaf_ro_ours = 0 | endif"
    " | unlet! b:vaf_user_ro"
    " | try | let g:vaf_r = writefile([&readonly && !get(b:, 'vaf_ro_ours') ? '1' : '0'],"
    " {answer}) | catch | finally | unlet! g:vaf_r | endtry"
)

# CoC's inlay hints (parameter names, inferred return types — coc-pyright's
# pyright.inlayHints.* etc.) render as virtual text once the follower's
# scratch buffer becomes a real, disk-backed one for the first time (the
# relock's `:e!`) and CoC attaches to it. The final buffer/file content is
# always correct — this is purely a rendering overlay CoC draws on top — but
# it makes the animation itself look like it typed garbage (live report,
# 2026-07-24: `os.listdir(root)` rendered as `os.listdir(path: root)`).
# `:CocDisable` stops the follower's OWN Vim process (a separate OS process
# per pane, never Alberto's real editing Vim) from handling Vim events at
# all, so CoC never attaches/computes hints for a buffer only the follower is
# driving. `:silent!` makes both commands a safe no-op for a Vim without CoC
# installed. Re-enabled only in hand_over(): that is the one place a human
# actually gets to type into the buffer themselves and wants real completion.
_COC_DISABLE = ":silent! CocDisable"
_COC_ENABLE = ":silent! CocEnable"

# goto_file's navigation, wrapped so a dirty target can't stall the pane.
#
# `:tab drop {file}` finishes by running `:rewind` on the arglist it just
# set, and `:rewind` calls Vim's abandon check against the buffer it has
# ALREADY landed on. When that buffer is modified, the check raises
# `E37: No write since last change (add ! to override)` — 51 characters,
# one more than the 49-column follower pane (`split-window -h` inside a
# 100-column window), so the message wraps and Vim turns it into a real,
# blocking "Press ENTER or type command to continue" hit-enter prompt in
# the user's pane. A dirty target is the normal state after an interrupt
# hand-off or a killed hook (see _with_unlocked: "A pause never relocks").
#
# The navigation itself has already completed by then — measured: the
# right tab is focused, the unsaved content is untouched, the tab count is
# unchanged. Only the trailing bookkeeping aborts, and nothing here needs
# it. So the fix is to swallow that ONE error and nothing else:
#
#   - `:tab drop!` is wrong: the bang reloads from disk and silently
#     DISCARDS the unsaved content (measured — it turns 3 of this file's
#     integration tests red).
#   - `:silent! tab drop` is wrong for a subtler reason: it also hides the
#     swap-file "ATTENTION" dialog, which blocks Vim exactly as it does
#     today but now with a blank screen — trading a visible stall for an
#     invisible one (measured: both forms leave Vim unresponsive; only
#     `silent!` leaves nothing on screen to explain why).
#   - `set hidden` around the drop does not even work: the same-file path
#     adds Vim's CCGD_MULTWIN flag, which skips the 'hidden' escape, so
#     E37 still fires — and a `|`-chained restore never runs after the
#     error, leaking `hidden` ON into what, in adopt mode, is the user's
#     own Vim (measured: &hidden left at 1 in all three dirty scenarios).
#
# The catch pattern is the documented `Vim(cmd):E37:` form and is narrow
# in both directions (measured against E17/E212/E325/E370, all of which
# still surface exactly as they do today).
#
# The SECOND stall the same line has to survive is Vim's swap-file
# `E325: ATTENTION` dialog, which `:tab drop` raises whenever the target
# has a `.file.swp` — another Vim holding it open (the everyday adopt-mode
# case: the user's own editor on the file Claude is writing), or a stale
# one left by a crash. At 49 columns the ATTENTION text is long enough to
# hit the `-- More --` pager BEFORE it even reaches the
# `[O]pen Read-Only, (E)dit anyway, (R)ecover, (Q)uit, (A)bort` question,
# so the pane is stuck twice over and every later keystroke answers a
# prompt instead of navigating (measured).
#
# Policy (Alberto, 2026-09-21): the follower answers `(E)dit anyway`,
# adopt mode included. It is safe because the follower never writes the
# file — its buffers are display-only and relocked read-only — so editing
# anyway cannot clobber the other Vim's work. Measured: the other Vim
# still `:w`s its unsaved changes to disk afterwards, and a stale swap
# file is left untouched on disk (we choose Edit, never (D)elete, so
# nobody's recovery data is destroyed).
#
# `SwapExists` + `v:swapchoice` is Vim's own designed hook for exactly
# this and answers the question without typing into a prompt at all. The
# two alternatives both work on screen and both LEAK (measured with a
# user `:e` of an unrelated swapped file afterwards):
#
#   - `set shortmess+=A` is global and never restored, so from then on the
#     USER's own `:e` opens a swapped file silently, with no warning at
#     all — strictly worse than the stall it fixes.
#   - `set noswapfile` is global too and costs the user crash recovery for
#     every file they open afterwards.
#
# So the hook is registered and torn down inside this one Ex line:
#
#   - It lives in its own augroup, created by an `augroup` command first.
#     `:autocmd {group} ...` does NOT create a missing group: it fails
#     with `E216` and leaves its own hit-enter prompt (measured), which is
#     the very failure being fixed.
#   - There is deliberately no bare `:autocmd!` anywhere in the line. It
#     would only ever apply to `vim_ai_follower_swap`, but if the
#     preceding `augroup` ever failed it would run in the DEFAULT group
#     and wipe every autocommand the user has. Nothing in the line needs
#     it: `finally` clears the group on every pass.
#   - The teardown is in `finally`, not after `endtry`, so it runs on the
#     E37 path and on an uncaught error too (both measured: no residue,
#     and a non-E37 error still reaches the user).
#   - `++once` bounds the one residual risk — this line being cut off
#     mid-flight, before `finally` — to a single auto-answered dialog
#     instead of the policy persisting in the user's Vim.
#
# Scoping is by TIME, not by pattern: the hook exists only for the
# duration of this drop, so a user `:e` of a swapped file afterwards
# still gets the normal dialog (measured). Matching on the path instead
# would mean escaping it into an autocmd pattern — the quoting hazard
# this line otherwise avoids entirely.
#
# And `:tab drop` is only the FALLBACK, for a file no buffer holds yet —
# the one case where reading disk is the intent. An already-loaded buffer
# is switched to by NUMBER and never re-read (measured 2026-09-23): the
# drop's trailing `:rewind` re-edits the buffer it lands on, and on an
# UNMODIFIED buffer — the normal state after a completed animation, whose
# relock ends in `:e!` — that is a silent reload from disk. By the time
# apply_edit navigates, Claude has already written the finished file, so
# the follower flashed it and then typed the diff (computed against the
# pre-edit snapshot) on top of it: duplicated lines until the relock's `:e!`
# snapped them back. The nvim backend's goto_file has the same rule.
#
# The number is resolved the way _WIPE_BUFFER does it — the path is read
# into a variable (_typed_path) and the buffer list is walked comparing `:p`
# names —
# because `bufnr()` and `:buffer {name}` take a PATTERN. A window already
# showing it is focused (`win_gotoid`, any tab); a loaded buffer with no
# window gets a new tab via `:tab sbuffer {nr}`, which does not re-read a
# loaded buffer; the current buffer is left alone. All three stay inside
# the try/SwapExists guard: `:tab sbuffer` on a listed-but-unloaded buffer
# does read the file, and can raise the ATTENTION dialog like the drop.
# The `g:` variables are unlet in `finally` (inert if a cut-off line leaves
# them, like _WIPE_BUFFER's).
#
# The drop and `:tab sbuffer` are `silent` (never `silent!`, see above: errors
# and the E37 exception still come through, measured by
# tests/test_integration_goto_file_e37.py and the swap tests). Both print the
# file-info message (`"<name>" 3L, 18B`) when they read a file, and for a
# file outside the cwd that name is the full path — shown on the command line
# the path handle exists to keep it off (measured 2026-09-29,
# tests/test_integration_cmdline_paths.py).
#
# The line is kept short on purpose (short `g:` names, win_gotoid's own
# return value picking between focus and `:tab sbuffer`): at 49 columns every
# ~49 characters is another screen row of command-line echo. The unnamed
# buffer needs no guard in the lookup: `fnamemodify('', ':p')` is the working
# DIRECTORY, trailing slash included, which no file path can equal.
_SWAP_GROUP = "vim_ai_follower_swap"
# The time-scoped `(E)dit anyway` answer, as the two halves every
# disk-reading line wraps itself in (the opening registers it; the closing
# runs in `finally`). See above for why each piece has this exact shape.
_SWAP_ANSWER_OPEN = (
    f'exe "augroup {_SWAP_GROUP}"'
    " | exe \"autocmd SwapExists * ++once let v:swapchoice = 'e'\""
    ' | exe "augroup END"'
)
_SWAP_ANSWER_CLOSE = f'exe "autocmd! {_SWAP_GROUP}" | exe "augroup! {_SWAP_GROUP}"'

# The completion relocks' `:e!` disk sync (see the lock protocol at the top),
# carrying the same scoped (E)dit-anyway answer. `:edit` re-runs the swap
# search, so on a buffer with swap ON (an adopted Vim's own buffers, the ones
# show_fresh gives swap back in an adopted Vim, a dedicated buffer a Read
# opened through `:tab drop`) whose file another Vim holds, the bare
# `:silent! e!` stalled INVISIBLY at the hidden ATTENTION dialog: its own
# `setlocal` never ran and the follower's next keys answered the dialog
# (measured 2026-09-28 at 49 columns, tests/test_integration_swap_reload.py).
# `silent!` stays on the edit itself: the relock never showed an error before.
_SYNC_FROM_DISK = (
    f":{_SWAP_ANSWER_OPEN} | try | silent! edit! | finally | {_SWAP_ANSWER_CLOSE} | endtry"
)
_RELOCK_SYNCED = _SYNC_FROM_DISK + " | " + _RELOCK[1:]
_RELOCK_READONLY_SYNCED = _SYNC_FROM_DISK + " | " + _RELOCK_READONLY[1:]
# The relocks that set the follower's own readonly; in an adopted Vim they
# also mark the follower's claim on it (see _NOTE_USER_READONLY).
_READONLY_RELOCKS = (_RELOCK_READONLY, _RELOCK_READONLY_SYNCED)
_OUR_READONLY_CLAIM = " | let b:vaf_ro_ours = 1"


def _vim_display_name(path_expr: str) -> str:
    """A Vim expression naming the file `path_expr` (a Vim expression for its
    full path) the way Vim names a file opened by RELATIVE path: relative to
    the working directory when it is under it, full otherwise. It is the
    argument show_fresh's `:file` and _GOTO_FILE's `:tab drop` hand to
    fnameescape().

    Why: Vim keeps a name it was given in full as typed, and shortens it only
    when a `:cd` happens. Measured 2026-09-29 on Vim 9.2 at 49 columns, cwd
    /private/tmp/vafn: `:file /private/tmp/vafn/sub/a.py` showed the tabline
    as `/p/t/v/s/a.py`, the full path in the statusline and
    `<te/tmp/vafn/sub/a.py" 1L, 6B written` for `:w`; `:file sub/a.py`
    shows `s/a.py`, `sub/a.py` and `"sub/a.py" 1L, 6B written`, exactly as
    `:e sub/a.py` does. The buffer's FULL name is the same either way, so the
    `:p` lookups (_FIND_BUFFER, _WIPE_BUFFER) still find it, and Vim re-bases
    the short name itself on any later `:cd`.

    `:.` compares against getcwd(), which is the PHYSICAL directory even when
    Vim was started from, or `:cd` to, a symlinked spelling of it (measured,
    macOS /tmp -> /private/tmp); the hook hands the backend a realpath
    (hooks._file_path), so the two line up without resolving anything here.

    The short name is kept only when it ROUND-TRIPS: its `:p` must be
    exactly the `:p` of the full path, which is the equation _FIND_BUFFER,
    _WIPE_BUFFER and _PROBE_BUFFER test (`==#`). Otherwise the full path is
    the name, as before relative names, and the lookups match it verbatim.
    Two measured ways the short form fails to come back (Vim 9.2, macOS):
    - A wrong-CASE path. `:.` shortens case-INSENSITIVELY on macOS: with cwd
      `…/Proj`, `…/proj/sub/a.py` shortens to `sub/a.py`, whose `:p` is
      `…/Proj/sub/a.py` — not the hook's `…/proj/sub/a.py` (the hook's
      realpath keeps the case it was given). Every lookup missed: the probe
      said "absent", show_fresh's pre-wipe missed so `:file` hit E95 and
      stacked unnamed tabs, max_tabs eviction never fired, `:w` gave E32.
    - A relative name starting with `~` (a directory literally called `~x`
      under the cwd): `:p` reads it as user x's home, not the file."""
    short = f"fnamemodify({path_expr}, ':.')"
    return (
        f"(fnamemodify({short}, ':p') ==# fnamemodify({path_expr}, ':p') ? {short} : {path_expr})"
    )


# `g:vaf_n` = the number of the buffer named `g:vaf_p`, or -1 (see the
# comment above _SWAP_GROUP).
_FIND_BUFFER = (
    "let g:vaf_n = get(filter(range(1, bufnr('$')), 'bufexists(v:val)"
    " && fnamemodify(bufname(v:val), '':p'') ==# fnamemodify(g:vaf_p, '':p'')'), 0, -1)"
)
# A line that must ACT on the target (goto, show_fresh's rename) leaves the
# path in g:vaf_p and is followed by this one, which proves it did: {answer}
# gets {nonce} only when the current buffer is the target. The Python side
# waits for it (_await_landing) and sends nothing more without it, so no
# lock, unlock, keystroke or relock can land on whatever buffer happened to
# be current (review, 2026-09-29: a failed goto let a Read lock the user's
# own modified buffer, and an Edit's relock `:e!` discard the user's unsaved
# text).
#
# A line of its own, after an Escape pair, never the acting line's tail.
# Measured 2026-09-29 (tests/test_integration_edit_no_reload.py's no-window
# case: one tab left, so `:tab sbuffer` opens a second): the goto line, 21-27
# screen rows at 49 columns, leaves Vim at a prompt that shows nothing new.
# As the line's tail, the answer never came, even after 90 s; as a separate
# line, its first screen row (49 characters) was swallowed and the rest ran
# as `ufname('%')…` (E492). Before the landing check, the line swallowed there
# was `:silent! CocDisable`, unnoticed. The Escape pair (_normal_mode's)
# dismisses the prompt, and the check then runs whole.
_ANSWER_IF_LANDED = (
    ":if get(g:, 'vaf_p', '') !=# ''"
    " && fnamemodify(bufname('%'), ':p') ==# fnamemodify(g:vaf_p, ':p')"
    " | try | let g:vaf_r = writefile([{nonce}], {answer}) | catch | endtry | endif"
    " | unlet! g:vaf_p g:vaf_r"
)
_GOTO_FILE = (
    ":{read} | if g:vaf_p !=# ''"
    f" | {_FIND_BUFFER} | {_SWAP_ANSWER_OPEN}"
    " | try"
    f" | if g:vaf_n < 0 | exe 'silent tab drop ' . fnameescape({_vim_display_name('g:vaf_p')})"
    " | elseif g:vaf_n != bufnr('%')"
    " | exe win_gotoid(get(win_findbuf(g:vaf_n), 0)) ? '' : 'silent tab sbuffer ' . g:vaf_n"
    " | endif"
    r" | catch /^Vim\%((\a\+)\)\=:E37:/"
    f" | finally | {_SWAP_ANSWER_CLOSE} | endtry | endif"
    " | unlet! g:vaf_h g:vaf_n"
)

# ensure_showing's disk re-read, for the Read/binary navigation where
# showing the file AS IT IS ON DISK is the whole point. goto_file no longer
# re-reads a loaded buffer (it runs before animations, where that is the
# bug above), so a file open and clean in the follower that something other
# than Claude's Edit rewrote — a formatter run through Bash, `sed -i`, a
# `git checkout` — would otherwise be shown stale, and locked read-only.
#
#   - `if !&modified`: a dirty buffer is typed-but-unsaved content (an
#     interrupt hand-off, a killed hook) and is never discarded here; that
#     also makes E37 unreachable, so no catch is needed.
#   - `:edit` re-runs Vim's swap-name search, and that DOES raise ATTENTION
#     on its own (measured 2026-09-23): when the user's editor opened the
#     file first it owns `.swp`, the follower took `.swo`, and the reload
#     drops `.swo` and probes `.swp` again. Unguarded, even the first Read
#     of such a file stalled on the dialog. So the reload carries the same
#     time-scoped `(E)dit anyway` answer as goto_file, torn down in
#     `finally`.
#   - `silent` (never `silent!`) keeps the long "<path>" NL, NB message from
#     wrapping into a hit-enter prompt at 49 columns without hiding errors.
#   - `:edit` works on a 'nomodifiable' buffer and keeps the option; it
#     resets 'readonly', which ensure_showing's lock re-asserts right after.
# show_fresh's swap re-enable for an ADOPTED Vim (Alberto's ruling,
# 2026-09-28): without a swap, a crash loses what the user types into the
# buffer and a second editor opening the file gets no ATTENTION. Turning swap
# on runs the swap check the rename's opt-out avoided; measured 2026-09-28 at
# 49 columns with an owner Vim holding `.swp`, a plain `:setlocal swapfile`
# left the ATTENTION block at `-- More --`, a scoped SwapExists answer does
# not fire for an option toggle, and `shortmess+=A` saved and restored in
# `finally` was silent, created `.swo`, and a second Vim opening the file
# still got its SwapExists. The nvim backend runs the same toggle over RPC.
#
# Prompt-proof when the toggle fails. A swap file Vim cannot create (E303:
# no writable 'directory') makes Vim set need_wait_return itself, so the line
# ended at "Press ENTER" at 49 columns and swallowed the `:filetype detect`
# and the keys after it. Measured 2026-09-28 with a forced E303: `silent!` on
# the setlocal, or a `catch`, hides the message but NOT the prompt; the
# trailing `redraw` (an intentional redraw clears need_wait_return) is what
# removes it, and with a writable 'directory' the swap is still created.
# The swap then stays unmade, which is the safe side.
_SWAP_BACK_ON = (
    ":let g:vaf_shortmess = &shortmess | set shortmess+=A"
    " | try | silent! setlocal swapfile"
    " | finally | let &shortmess = g:vaf_shortmess | unlet g:vaf_shortmess | endtry"
    " | redraw"
)

# show_fresh's read of the file into the buffer it just renamed, cleared again
# at once. Renaming a buffer onto a path (`:file`, and nvim's buf_set_name)
# marks it "not edited", and Vim refuses a plain `:w` of a not-edited buffer
# over an existing file with `E13: File exists (add ! to override)`; only a
# read or a write clears the mark (measured 2026-09-28, Vim and nvim alike).
# Found recording the demo: the hand-off cue said ":w releases" and only
# `:w!` worked. The read also gives the buffer the file's timestamp, so a
# plain `:w` after something else rewrote the file asks "changed since reading
# it" instead of overwriting silently (a not-edited buffer skipped that check).
#
#   - It runs on show_fresh's rename line: Vim does not redraw between the
#     commands of one command line, so the file's content is never on screen
#     (the rename still exists so that nothing else ever is).
#   - `noautocmd`: no BufRead autocommands, so plugins (CoC) attach no
#     earlier than they did (the relock's `:e!`), and none can force a redraw
#     mid-line. `:filetype detect` runs afterwards anyway.
#   - swap is already off (show_fresh sets `noswapfile` before the rename), so
#     the read raises no ATTENTION; an adopted Vim's swap goes back on after.
#   - `silent!`: a file that cannot be read must not leave a prompt at 49
#     columns; the buffer then just stays not edited, as before.
#   - `%d _` into the black-hole register, leaving the user's registers alone.
_READ_THEN_CLEAR = "noautocmd silent! edit! | silent! %d _"

_RELOAD_IF_CLEAN = (
    f":{_SWAP_ANSWER_OPEN}"
    " | try | if !&modified | silent edit | endif"
    f" | finally | {_SWAP_ANSWER_CLOSE} | endtry"
)

# reload_from_disk's re-read: _RELOAD_IF_CLEAN without the clean check, for a
# buffer that holds only follower text the hook has decided to discard (see
# hooks._animate_edit, after a completed catch-up). Same scoped swap answer
# and the same `silent`, for the same reasons.
_RELOAD_DISCARDING = (
    f":{_SWAP_ANSWER_OPEN} | try | silent edit! | finally | {_SWAP_ANSWER_CLOSE} | endtry"
)


# probe_buffer's read-back: the one place this keystroke-driven backend asks
# Vim a question. Vim writes the lines of the buffer file_path names into a
# probe file that Python polls for. It looks the buffer up by NUMBER exactly
# like _GOTO_FILE (never a name pattern) and never navigates: `:tab sbuffer`
# on an unloaded buffer is the very disk read the question exists to avoid.
#
#   - getbufline() of an unloaded buffer (listed, but its tab was closed under
#     'nohidden') and of a missing one (-1) is an empty list, while a loaded
#     buffer always has at least one line — so "no lines" already means
#     "absent", with no status field to keep in sync (classify_buffer);
#   - a nonce closes the dump, so a half-written file, or one left by an
#     older probe that timed out and landed late, is never read as this
#     probe's answer;
#   - the lines themselves, not a `sha256()` of them: that needs +cryptv, and
#     an E117 at 49 columns is a hit-enter prompt that would eat the
#     animation's keystrokes;
#   - everything inside `try | … | catch | finally | … | endtry`, with an
#     empty catch-all: no failure may leave a prompt in the pane. Measured
#     2026-09-25 on a real Vim at 49 columns: an unwritable probe directory
#     raised `E482: Can't create file <path>`, whose long path wrapped into
#     "Press ENTER or type command to continue" — and in an adopted Vim nothing
#     is sent after the probe to dismiss it. Swallowed, the error just means
#     no answer, which the poll turns into "unknown" at the timeout. `silent!`
#     was not used: it scopes to one command, and the lookup's filter() can
#     fail too;
#   - `let g:vaf_r = writefile(…)`, never `call writefile(…)`: measured on the
#     same Vim, a `:call` whose function fails skips the REST OF THE LINE, so
#     `catch`/`finally`/`endtry` were never read and the unclosed `:try` left
#     the command line waiting for more (a `:  ` continuation prompt) —
#     no hit-enter message, but every later keystroke fed an open block. A
#     failing `:let` hands over to the catch and the line completes.
_PROBE_BUFFER = (
    ":try | {read} | if g:vaf_p !=# '' | "
    + _FIND_BUFFER
    + " | let g:vaf_r = writefile(getbufline(g:vaf_n, 1, '$') + [{nonce}], {probe}) | endif"
    " | catch | finally | unlet! g:vaf_h g:vaf_p g:vaf_n g:vaf_r | endtry"
)
# How long probe_buffer waits for Vim's answer before calling it "unknown"
# (the hook's safe side: a retype, or in an adopted Vim, leaving it alone).
# Measured 2026-09-24 against a real tmux+vim at load 4-6: a
# median of ~50 ms and a worst of ~100 ms per probe, for 50- and 2000-line
# buffers alike, so this only runs out when Vim is stuck or the machine is
# drowning, and then retyping is the right call anyway.
_PROBE_TIMEOUT_SECONDS = 2.0
_PROBE_POLL_SECONDS = 0.01
# How long a navigating line waits for Vim's landing confirmation
# (_await_landing). Far longer than a probe's: the goto line is ~1000 typed
# characters, and Vim echoes each one; measured 2026-09-29, a follower Vim
# running a 15 ms timer (tests/test_integration_edit_no_reload.py's
# observer) was still echoing the line after 2 s at load 3. Running out
# means the edit is skipped, so this is sized for a slow Vim; the one case
# that always waits it out is a navigation that cannot land (a stale HOME
# answer), and that one heals itself (_distrust_home).
_LANDING_TIMEOUT_SECONDS = 10.0

logger = logging.getLogger("vim_ai_follower")


def _probe_path(pane_id: str) -> Path:
    """Where Vim writes probe_buffer's answer for this pane."""
    return cache.CACHE_DIR / f"probe-{pane_id.lstrip('%')}.txt"


def _readonly_answer_path(pane_id: str) -> Path:
    """Where _RESTORE_READONLY leaves user_readonly's answer for this pane."""
    return cache.CACHE_DIR / f"readonly-{pane_id.lstrip('%')}.txt"


def _read_probe(probe: Path, nonce: str) -> list[str] | None:
    """The buffer lines the probe reported, or None while the file is not
    (yet) this probe's complete answer."""
    try:
        raw = probe.read_bytes()
    except OSError:
        return None
    # writefile() ends every item with a newline, the last one included.
    items = raw.decode("utf-8", errors="replace").split("\n")[:-1]
    if not items or items[-1] != nonce:
        return None
    return items[:-1]


def _vim_string(value: str) -> str:
    """`value` as a Vim single-quoted string literal.

    Total, and cheaply so: Vim's single-quoted strings process no backslash
    escapes at all, so the only character needing any handling is `'`, which
    doubles. Every other byte survives verbatim — including the `#`, `%`,
    `$`, `*`, `[`, `]`, `{` and `}` that Vim's command line and its
    buffer-name patterns would otherwise eat."""
    return "'" + value.replace("'", "''") + "'"


# How long a path handle (see _typed_path) is kept before a later call sweeps
# it, whichever pane it was for. Vim deletes a handle on the same line that
# reads it, well under a second after the send even at load 15; this only
# bounds the ones no Vim ever read (a cut-off line, a Vim on another HOME, a
# pane that died). Sweeping one early fails safe, never to another file: a
# missing handle reads as '' (_typed_path) and the line then acts on nothing,
# and a line that had to act says so by not answering (_ANSWER_IF_LANDED).
_PATH_HANDLE_MAX_AGE_SECONDS = 600.0

# The characters a HOME-relative cache path may have to be typed as
# `expand('~/…', 1)`: expand() would also expand a wildcard (`*?[{`), `$NAME`,
# a backtick command or a second `~` in it. The real one is
# `.cache/claude-vim-follower`. The `1` (nosuf) matters as much: without it
# 'wildignore' applies, and measured 2026-09-29 (Vim 9.2) a Vim with
# `wildignore=*/.cache/*` expands the handle to '' (E484 on the read).
_EXPAND_SAFE = re.compile(r"[A-Za-z0-9._/-]+")

# The one-time question to a pane's Vim: create {marker} (a `~/…` path under
# the hook's cache directory) through Vim's own `~`. It lands in the hook's
# CACHE_DIR only when that Vim's HOME is the hook's HOME. Inside try/catch and
# via `let`, like _PROBE_BUFFER: no failure may leave a prompt.
_HOME_CHECK = (
    ":try | let g:vaf_r = writefile([], expand({marker}, 1))"
    " | catch | finally | unlet! g:vaf_r | endtry"
)


def _vim_shares_home(pane_id: str, cache_relative: str) -> bool:
    """Whether `~` in this pane's Vim is the hook's HOME, so a cache file can
    be typed as `~/{cache_relative}/…`.

    They can differ: a dedicated follower's Vim gets the tmux server's
    environment and an adopted one the user's shell's, while the hook has
    Claude Code's (a test, or `HOME=… claude`, sets one and not the other).
    Measured 2026-09-29 (Vim 9.2): Vim's `~` is its $HOME with symlinks
    resolved, so a HOME spelled through /tmp still names the same directory.

    Asked once per pane process (tmux's `pane_pid`) and remembered in
    `home-<pane>`, which holds that pid and the marker file the question
    asked Vim to create. The answer is "yes" exactly while that marker exists
    in the hook's CACHE_DIR: a Vim with another HOME creates it somewhere
    else (or nowhere, when that HOME has no cache directory). An answer that
    arrives after the wait is still picked up by the next call. Until then,
    and whenever tmux cannot say which process the pane runs, the absolute
    path is typed.

    The pane process is the Vim itself for a dedicated follower (a new Vim is
    a new pane) but the user's shell for an adopted one, so a Vim restarted
    in that shell inherits the answer: right unless it was started with
    another HOME (`HOME=… vim`). Then its handle reads come back '', the
    navigation does not land, nothing more is sent (_ANSWER_IF_LANDED), and
    the unanswered question drops the stale "yes" (_distrust_home) so the
    next call asks this Vim again. Asking every Vim would take a round trip
    per call; tmux names no foreground pid to key on."""
    pane = TmuxPane(pane_id=pane_id)
    pane_pid = pane.pane_pid()
    if pane_pid is None:
        return False
    directory = cache.CACHE_DIR
    record = directory / f"home-{pane_id.lstrip('%')}"
    try:
        recorded_pid, marker = record.read_text().split()
    except (OSError, ValueError):
        recorded_pid, marker = "", ""
    if recorded_pid == str(pane_pid):
        return (directory / marker).exists()
    # Every marker of this pane goes, not just the recorded one: two hooks
    # asking at once each create one, and only the last is recorded.
    prefix = f"h{pane_id.lstrip('%')}-"
    for old in directory.glob(prefix + "*"):
        old.unlink(missing_ok=True)
    marker = f"{prefix}{secrets.token_hex(3)}"
    record.write_text(f"{pane_pid} {marker}\n")
    pane.send_text(_HOME_CHECK.format(marker=_vim_string(f"~/{cache_relative}/{marker}")))
    pane.send_key("Enter")
    deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
    while not (directory / marker).exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(_PROBE_POLL_SECONDS)
    return True


def _cache_file(pane_id: str, path: Path) -> str:
    """A Vim expression naming `path`, a file in the cache directory, as short
    as is safe: `expand('~/.cache/claude-vim-follower/…', 1)` when this pane's
    Vim shares the hook's HOME, the absolute literal otherwise. Typed lines
    are echoed on Vim's command line (see _typed_path), and HOME is often a
    long absolute directory of its own (in the demo, one under /tmp).

    `~` needs expand(): measured 2026-09-29 (Vim 9.2), readfile() and
    writefile() take `~/…` literally (E484/E482)."""
    directory = cache.CACHE_DIR
    try:
        cache_relative = Path(os.path.realpath(directory)).relative_to(
            os.path.realpath(Path.home())
        )
    except ValueError:
        return _vim_string(str(path))
    if not _EXPAND_SAFE.fullmatch(str(cache_relative)) or not _EXPAND_SAFE.fullmatch(path.name):
        return _vim_string(str(path))
    if not _vim_shares_home(pane_id, str(cache_relative)):
        return _vim_string(str(path))
    return f"expand('~/{cache_relative}/{path.name}',1)"


def _typed_path(pane_id: str, file_path: str, var: str) -> str:
    """Vim statements setting `var` to `file_path` without spelling it: the
    path is written to a handle file in the cache directory and the
    typed line reads it back (`readfile()`) and deletes it, naming the handle
    HOME-relative when it can (_cache_file). A handle Vim cannot read (a
    stale HOME answer, one swept under a stalled Vim) gives '', and every
    line using it then acts on nothing.

    `filereadable()` first, never a try around the read: measured 2026-09-29
    (Vim 9.2), readfile()'s E484 inside a one-line `try | … | catch |
    endtry` aborts the REST of the line, `catch` and `endtry` included, so
    the error shows (a hit-enter prompt at 49 columns, naming the missing
    handle) and the try is left open.

    Why: this backend TYPES its Ex commands, and Vim echoes a typed command on
    its command line before running it. With the path spelled in the line,
    every edit and navigation flashed the target's absolute path in the
    follower's footer, up to five screen rows of it at 49 columns (found on a
    demo recording, 2026-09-29; tests/test_integration_cmdline_paths.py logs
    every command-line change). The line now shows only the cache handle.

    - One handle PER CALL (a random suffix), never a fixed per-pane file: Vim
      runs a line some time after the send returns, so a fixed file could be
      rewritten by the next call first — close_tab's eviction followed by
      show_fresh's pre-wipe would then both wipe the NEW file, and the evicted
      one would stay. A handle is only ever read by the line that names it,
      which deletes it right after (`delete()`), and is created exclusively
      (`xb`), so two calls never share one.
    - Byte-exact: the file holds `os.fsencode(file_path)` with no trailing
      newline, and `readfile(…, 'b')` joined on "\n" gives back exactly those
      bytes, a newline in the path included (binary mode keeps a CR and adds
      no item for a missing final newline). A NUL cannot occur in a path."""
    directory = cache.CACHE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in directory.glob("p*-*"):
        if not _HANDLE_NAME.fullmatch(old.name):
            continue
        # Another hook may sweep the same handle between glob and unlink.
        with contextlib.suppress(FileNotFoundError):
            if now - old.stat().st_mtime > _PATH_HANDLE_MAX_AGE_SECONDS:
                old.unlink()
    prefix = f"p{pane_id.lstrip('%')}-"
    while True:
        handle = directory / f"{prefix}{secrets.token_hex(3)}"
        try:
            with handle.open("xb") as stream:
                stream.write(os.fsencode(file_path))
        except FileExistsError:
            continue
        return (
            f"let g:vaf_h = {_cache_file(pane_id, handle)}"
            f" | let {var} = filereadable(g:vaf_h) ? join(readfile(g:vaf_h,'b'),\"\\n\") : ''"
            " | call delete(g:vaf_h)"
        )


# A path handle's name (_typed_path): `p<pane number>-<6 hex>`.
_HANDLE_NAME = re.compile(r"p[0-9]+-[0-9a-f]{6}")


class NavigationFailed(RuntimeError):
    """Vim did not confirm landing on the target (_ANSWER_IF_LANDED), so the
    call stopped before sending anything that acts on the current buffer."""


def _goto_answer_path(pane_id: str) -> Path:
    """Where a line that must land on its target confirms it did."""
    return cache.CACHE_DIR / f"landed-{pane_id.lstrip('%')}.txt"


def _confirm_landing(pane: TmuxPane, file_path: str) -> None:
    """Ask Vim whether the line just sent landed on file_path (the path it
    left in g:vaf_p) and raise NavigationFailed unless it says so."""
    answer = _goto_answer_path(pane.pane_id)
    answer.parent.mkdir(parents=True, exist_ok=True)
    answer.unlink(missing_ok=True)
    nonce = secrets.token_hex(4)
    # Clears a prompt the acting line can leave (see _ANSWER_IF_LANDED).
    pane.send_key("Escape")
    pane.send_key("Escape")
    pane.send_text(
        _ANSWER_IF_LANDED.format(nonce=_vim_string(nonce), answer=_cache_file(pane.pane_id, answer))
    )
    pane.send_key("Enter")
    _await_landing(pane.pane_id, answer, nonce, file_path)


def _distrust_home(pane_id: str) -> None:
    """Forget a "yes" to the HOME question (see _vim_shares_home) after a
    question to the pane's Vim went unanswered: if its `~` is no longer the
    hook's (an adopted pane's shell restarted Vim with another HOME), every
    `~/…` name it is typed misses, and only a new question finds out. A
    recorded "no" is kept: asking again costs a full wait for nothing."""
    record = cache.CACHE_DIR / f"home-{pane_id.lstrip('%')}"
    try:
        _pid, marker = record.read_text().split()
    except (OSError, ValueError):
        return
    if (cache.CACHE_DIR / marker).exists():
        logger.warning("pane %s's Vim did not answer; asking about its HOME again", pane_id)
        record.unlink(missing_ok=True)


def _await_answer(path: Path, nonce: str, timeout: float) -> list[str] | None:
    """What Vim wrote to `path` closed by `nonce` (_read_probe), waited for
    up to `timeout` seconds; None when it never came."""
    deadline = time.monotonic() + timeout
    while (lines := _read_probe(path, nonce)) is None:
        if time.monotonic() >= deadline:
            return None
        time.sleep(_PROBE_POLL_SECONDS)
    return lines


def _await_landing(pane_id: str, answer: Path, nonce: str, file_path: str) -> None:
    """Raise NavigationFailed unless Vim confirmed (_ANSWER_IF_LANDED) that the
    current buffer is file_path."""
    landed = _await_answer(answer, nonce, _LANDING_TIMEOUT_SECONDS) is not None
    answer.unlink(missing_ok=True)
    if not landed:
        _distrust_home(pane_id)
        logger.warning("the follower's Vim did not land on %s; nothing more was sent", file_path)
        raise NavigationFailed(file_path)


# Wipe the buffer holding a given file, resolved by NUMBER rather than by
# name. This is the eviction primitive close_tab uses and the pre-wipe
# show_fresh does before it renames a buffer onto the same path.
#
# `:bwipeout {name}` does NOT take a file name: it takes a buffer-name
# pattern (`:h {bufname}`). A real path containing `[`, `]`, `{` or `}`
# therefore matches nothing, and `:silent!` swallows the E94 that says so —
# measured 2026-09-22 against a real tmux+vim: the "evicted" buffer and its
# tab both survive, so max_tabs stops capping anything. `#`, `%` and `$` are
# worse still: those are expanded on the command line itself (alternate
# file, current file, environment variable), so the wipe targets some other
# buffer entirely. `bufnr()` is no escape — it pattern-matches by the same
# rules (the nvim backend used it until 2026-09-22 and resolved
# `app/[slug]/page.tsx` to its sibling `app/s/page.tsx`; see its
# `_buffer_number`).
#
# So the path never reaches a pattern at all. Vim reads it from a handle
# file into a string (see _typed_path), and the buffer list is walked comparing FULL
# names; `:p` normalizes both sides, so `/a/./b.py` and `/a/b.py` still
# match. Only an exact hit is wiped, and a miss wipes nothing — which is
# load-bearing, not merely tidy: a bare `:bwipeout!` with no number would
# wipe the CURRENT buffer.
#
# The two `g:` variables are unlet in the same line. They exist because the
# comparison target has to be referenced from inside filter()'s expression
# STRING, and nesting a path through two levels of Vim string quoting is the
# hazard this whole constant exists to avoid. A line cut off mid-flight can
# leave them behind; they are inert, and the next call overwrites them.
#
# Never follow this with `:tabclose`. Wiping a buffer that is its tab's only
# window already closes that tab, and the "safety" close then lands on
# whichever neighbour received focus and eats an innocent one (live eviction
# bug, 2026-07-15).
_WIPE_BUFFER = (
    # A failed read leaves '', which no buffer's `:p` equals ('' itself would
    # be the working directory, which a directory buffer can be named).
    ":{read} | let g:vaf_wipe_name = g:vaf_wipe_name ==# ''"
    " ? '' : fnamemodify(g:vaf_wipe_name, ':p')"
    " | let g:vaf_wipe_nr = get(filter(range(1, bufnr('$')),"
    ' \'bufexists(v:val) && bufname(v:val) !=# ""'
    ' && fnamemodify(bufname(v:val), ":p") ==# g:vaf_wipe_name\'), 0, -1)'
    " | if g:vaf_wipe_nr > 0 | exe 'silent! bwipeout! ' . g:vaf_wipe_nr | endif"
    " | unlet! g:vaf_h g:vaf_wipe_name g:vaf_wipe_nr"
)


@dataclass(frozen=True)
class TmuxVimFollower:
    """Follower backend that drives a real Vim instance in a tmux pane via
    simulated keystrokes (tmux send-keys)."""

    pane_id: str
    pace_seconds: float = DEFAULT_PACE_SECONDS
    window_id: str = ""

    def is_alive(self) -> bool:
        return TmuxPane(pane_id=self.pane_id).running_command() == "vim"

    def _live_pace(self) -> float:
        """Re-read the current speed from FollowerState so a running
        animation reacts to Ctrl+a +/- at its next line boundary, instead
        of only on the animation started after the toggle. Falls back to
        the pace this follower was constructed with when there's no
        window to read state for (or no state was ever written)."""
        if not self.window_id:
            return self.pace_seconds
        state = FollowerState.read(self.window_id)
        if state is None:
            return self.pace_seconds
        return config.pace_seconds_for(state.speed)

    def _is_adopted(self) -> bool:
        """The Vim is the user's own editor (FollowerState, as on nvim). Only
        swap handling asks: this backend locks an adopted Vim like a
        dedicated one (see CLAUDE.md)."""
        if not self.window_id:
            return False
        state = FollowerState.read(self.window_id)
        return state is not None and state.adopted

    def _normal_mode(self, pane: TmuxPane) -> None:
        # Two Escapes return to Normal mode from any mode. Never Ctrl-\
        # Ctrl-N here: with a plugin-loaded Vim (vim-visual-multi + CoC)
        # and a pending hit-enter prompt, the pair reproducibly corrupts
        # the following command line — observed live and in a scripted
        # repro as a junk buffer named "<file><Plug>(VM-Hls)" and as the
        # :tab drop being swallowed outright. The exact feedkeys chain is
        # VM-internal; plugin-free Vims cannot reproduce it, so the
        # regression check lives in scripts/repro-plugin-preamble.sh
        # against the real config. Escape is idempotent and single-key:
        # whatever consumes the first, the second still lands as Escape.
        pane.send_key("Escape")
        pane.send_key("Escape")

    def _unlock_line(self) -> str:
        return _ADOPTED_UNLOCK_FOR_ANIMATION if self._is_adopted() else _UNLOCK_FOR_ANIMATION

    def _send_reload_and_lock(self, pane: TmuxPane, reload: str) -> None:
        """A disk re-read (`reload`, which resets 'readonly'), then the
        follower's readonly lock. In an adopted Vim the user's readonly is
        noted first and the lock claims the option (see _NOTE_USER_READONLY)."""
        if self._is_adopted():
            pane.send_text(f":{_NOTE_USER_READONLY} | {reload[1:]}")
            pane.send_key("Enter")
            pane.send_text(_ADOPTED_LOCK_READONLY)
        else:
            pane.send_text(reload)
            pane.send_key("Enter")
            pane.send_text(_LOCK_READONLY)
        pane.send_key("Enter")

    def goto_file(self, file_path: str) -> None:
        """The defensive preamble: land on the tab showing file_path (by
        name, immune to the user closing/reordering tabs), opening one if
        missing. A buffer that already holds the file is switched to by
        number and NEVER re-read from disk — only a file no buffer holds
        goes through `:tab drop` (see _GOTO_FILE for the reload bug that
        rule fixes). Wrapped in _GOTO_FILE's guards so neither a modified
        target (E37) nor a swap file on the target (the ATTENTION dialog,
        answered `(E)dit anyway`) can leave a blocking prompt in the pane
        — see that constant for why the bang, `:silent!`, 'hidden',
        'shortmess' and 'noswapfile' are all wrong.

        This is the single navigation preamble every other method calls,
        so both guards cover show_fresh, ensure_showing, apply_edit,
        reload_and_relock, rewrite_buffer, resume and close_tab at once.

        file_path reaches Vim as a string read from a handle file
        (_typed_path: never spelled on the command line, which Vim echoes)
        run through Vim's own `fnameescape()`, never raw: as a bare `tab drop`
        argument `#`/`%` expanded to the alternate/current file, `$NAME` to
        an environment variable, a space split it into two files and a glob
        opened a matching sibling (all measured 2026-09-22). `:exe` keeps
        the E37 catch intact — the error still reads `Vim(drop):E37:`.

        Waits for Vim to confirm it landed (_ANSWER_IF_LANDED) and raises
        NavigationFailed otherwise, so no caller sends a lock, an unlock, an
        animation or a relock onto whatever buffer is current instead."""
        pane = TmuxPane(pane_id=self.pane_id)
        self._normal_mode(pane)
        pane.send_text(_GOTO_FILE.format(read=_typed_path(self.pane_id, file_path, "g:vaf_p")))
        pane.send_key("Enter")
        _confirm_landing(pane, file_path)

    def reload_and_relock(self, file_path: str) -> None:
        """Des-interrupt: discard the user's unsaved typing by reloading the
        file Claude wrote, then resume following it."""
        self.goto_file(file_path)
        pane = TmuxPane(pane_id=self.pane_id)
        # Not a bare `:e!`: on a buffer with swap on whose file another Vim
        # holds, it put up the whole ATTENTION dialog (see _SYNC_FROM_DISK).
        self._send_reload_and_lock(pane, _RELOAD_DISCARDING)

    def reload_from_disk(self, file_path: str) -> None:
        """Ground the buffer on the file as it is on disk, discarding what it
        holds, and lock it. Only for a buffer holding nothing but follower
        text: a catch-up that just completed (hooks._animate_edit). Without
        it that buffer stays modified while disk moved on, and the next
        `:checktime` raises the blocking W12 dialog."""
        self.goto_file(file_path)
        pane = TmuxPane(pane_id=self.pane_id)
        self._send_reload_and_lock(pane, _RELOAD_DISCARDING)

    def rewrite_buffer(self, file_path: str, content: str) -> AnimationResult:
        """Instantly (pace 0) rebuild the buffer to `content` — the
        des-interrupt replay needs the buffer back at the interrupt-point
        state, discarding the user's unsaved typing, before the remainder
        resumes at live pace. Leaves the buffer unlocked: resume() runs
        immediately after and owns the relock."""
        self.goto_file(file_path)
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(self._unlock_line())
        pane.send_key("Enter")
        pane.send_text(":%d")
        pane.send_key("Enter")
        lines = tuple(content.splitlines())
        if not lines:
            return AnimationResult("completed", 0)
        # `:%d` above wiped the buffer, so this rebuild starts from nothing.
        return run_lines(pane, self.window_id, lines, 0.0, file_path=file_path, base_content="")

    def close_tab(self, file_path: str) -> None:
        """Evict `file_path`: wipe its buffer, which closes the tab it was
        the only window of. On the last remaining tab the wipe just leaves
        an empty buffer, which is fine.

        There is deliberately no goto_file preamble. It was never
        load-bearing — _WIPE_BUFFER resolves a buffer NUMBER, and wiping by
        number closes the right tab from wherever the cursor happens to be,
        the same thing the nvim backend measured for its own close_tab — and
        it was actively harmful: `:tab drop` on a path Vim does not already
        hold OPENS a tab for it, so an eviction whose wipe then missed
        ADDED a tab instead of removing one. With the name-pattern wipe that
        preceded it, that pair is how the tab count climbed past max_tabs
        while the follower's own bookkeeping stayed pinned at the limit."""
        pane = TmuxPane(pane_id=self.pane_id)
        self._normal_mode(pane)
        pane.send_text(
            _WIPE_BUFFER.format(read=_typed_path(self.pane_id, file_path, "g:vaf_wipe_name"))
        )
        pane.send_key("Enter")

    def ensure_showing(self, file_path: str) -> None:
        """The Read/binary entry point: show the file as it is ON DISK. It
        is the one navigation that re-reads a loaded buffer (clean only, see
        _RELOAD_IF_CLEAN); goto_file itself never does, because it also runs
        before every animation."""
        self.goto_file(file_path)
        pane = TmuxPane(pane_id=self.pane_id)
        # Locked by default so a stray keystroke into this pane can't corrupt
        # the buffer: our own animation is indistinguishable from real
        # typing at the tty level, so it must explicitly unlock around itself.
        self._send_reload_and_lock(pane, _RELOAD_IF_CLEAN)

    def probe_buffer(self, file_path: str, content: str) -> BufferProbe:
        """What Vim's buffer for file_path holds relative to `content`, the
        base an edit script is about to be typed onto (see BufferProbe). The
        probe is a round-trip (_PROBE_BUFFER): Vim dumps the buffer to a file
        and this polls for it. No answer within _PROBE_TIMEOUT_SECONDS is
        "unknown"."""
        probe = _probe_path(self.pane_id)
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.unlink(missing_ok=True)
        nonce = secrets.token_hex(8)
        pane = TmuxPane(pane_id=self.pane_id)
        self._normal_mode(pane)
        pane.send_text(
            _PROBE_BUFFER.format(
                read=_typed_path(self.pane_id, file_path, "g:vaf_p"),
                nonce=_vim_string(nonce),
                probe=_cache_file(self.pane_id, probe),
            )
        )
        pane.send_key("Enter")
        lines = _await_answer(probe, nonce, _PROBE_TIMEOUT_SECONDS)
        if lines is None:
            logger.warning(
                "no answer from the follower's Vim about %s within %.1fs",
                file_path,
                _PROBE_TIMEOUT_SECONDS,
            )
            _distrust_home(self.pane_id)
            return "unknown"
        probe.unlink(missing_ok=True)
        return classify_buffer(lines, content)

    def user_readonly(self, file_path: str) -> bool:
        """See Follower.user_readonly. A dedicated follower never asks: its
        readonly is never the user's. In an adopted Vim, the answer the last
        readonly restore left (_RESTORE_READONLY), waited for up to
        _PROBE_TIMEOUT_SECONDS; every hand-off follows one (an interrupt's, or
        hand_over's), which deletes the old answer before it is sent. No
        answer is False, the plain cue."""
        if not self._is_adopted():
            return False
        answer = _readonly_answer_path(self.pane_id)
        deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
        while True:
            try:
                text = answer.read_text()
            except OSError:
                text = ""
            if text.endswith("\n"):
                return text == "1\n"
            if time.monotonic() >= deadline:
                logger.warning("no readonly answer from the follower's Vim about %s", file_path)
                _distrust_home(self.pane_id)
                return False
            time.sleep(_PROBE_POLL_SECONDS)

    def _send_restore_readonly(self, pane: TmuxPane) -> None:
        answer = _readonly_answer_path(self.pane_id)
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.unlink(missing_ok=True)
        pane.send_text(_RESTORE_READONLY.format(answer=_cache_file(self.pane_id, answer)))
        pane.send_key("Enter")

    def _with_unlocked(
        self, relock: str, run: Callable[[TmuxPane], AnimationResult]
    ) -> AnimationResult:
        """Owns the lock protocol shared by every animation entry point.
        'paste' suppresses autoindent/smartindent/cindent for the duration:
        without it, each Enter in insert mode auto-inserts indentation that
        then stacks with the leading whitespace already in our own lines.
        An interrupted animation hands the buffer to the user — it stays
        modifiable. A pause never reaches here: it loops inside run_ops/
        run_lines and only returns once the run has actually completed or
        been interrupted, so 'relock' only ever fires on a genuinely
        completed outcome. Callers own the exact relock string, which
        prepends a silent `:e!` disk sync (see apply_edit/show_fresh/
        resume) — the buffer's name matches the file Claude just wrote, so
        the reload is visually a no-op, but it grounds the buffer's
        timestamp and clears the W11 staleness that an unsynced retype
        would otherwise leave behind."""
        adopted = self._is_adopted()
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(_COC_DISABLE)
        pane.send_key("Enter")
        pane.send_text(_ADOPTED_UNLOCK_FOR_ANIMATION if adopted else _UNLOCK_FOR_ANIMATION)
        pane.send_key("Enter")
        result = run(pane)
        if result.outcome != "interrupted":
            if adopted and relock in _READONLY_RELOCKS:
                relock += _OUR_READONLY_CLAIM
            pane.send_text(relock)
            pane.send_key("Enter")
        if adopted:
            # After the relock, whose `:e!` resets 'readonly' (and on an
            # interrupt, which hands over the buffer without one).
            self._send_restore_readonly(pane)
        return result

    def apply_edit(
        self, file_path: str, ops: list[EditOp], before: str | None = None
    ) -> AnimationResult:
        """`before` is the buffer content these ops were computed against — the
        base run_ops needs to record the crash-fallback `partial` on a pause.

        Why the caller passes it instead of this backend reading it: the tmux
        backend drives Vim through `tmux send-keys` only. TmuxPane is
        write-only for buffer content (send_text/send_key/kill/title — there is
        no buffer-dump helper and none of `:redir`, `capture-pane` or a
        temp-file round-trip exists anywhere in src/), so reading the buffer
        back would mean inventing a blocking read against a keystroke-driven
        editor mid-animation. hooks._animate_edit already holds the exact value
        (`load_snapshot`, the same string it diffs `after` against and the same
        one its own interrupt path persists), so it hands it over.

        None keeps the old behavior — the pending is saved with partial=None
        and the consumer falls back to the live buffer — for any caller that
        genuinely has no snapshot."""
        self.goto_file(file_path)
        return self._with_unlocked(
            _RELOCK_SYNCED,
            lambda pane: run_ops(
                pane,
                self.window_id,
                ops,
                self._live_pace,
                file_path=file_path,
                on_resume=lambda: self.goto_file(file_path),
                base_content=before,
            ),
        )

    def show_fresh(self, file_path: str, content: str, in_new_tab: bool = False) -> AnimationResult:
        pane = TmuxPane(pane_id=self.pane_id)
        # Deliberately never `:e file_path` here: that would load the file's
        # real (already-written) content and flash the finished result on
        # screen before the wipe+retype, spoiling the "watch it type" effect.
        # Instead the current buffer is wiped and renamed in place, so the
        # real content is never displayed before we type it back in.
        self._normal_mode(pane)
        # Same by-number wipe as close_tab, and for the same reason: a
        # name-pattern miss here leaves the old buffer alive, and the
        # `:file` below then hangs a SECOND buffer off the same path.
        pane.send_text(
            _WIPE_BUFFER.format(read=_typed_path(self.pane_id, file_path, "g:vaf_wipe_name"))
        )
        pane.send_key("Enter")
        # Everything from here to the rename is ONE line, run only when the
        # path read succeeded, and nothing after it is sent until Vim confirms
        # the rename landed (_confirm_landing): a failed read must not open a
        # stray tab, turn swap off for the user's current buffer, or let the
        # `%d` and the typing below run on it.
        #
        # Opt the buffer out of swap BEFORE naming it: renaming a swap-enabled
        # buffer runs the swap check for the new name, and when a live Vim
        # holds the file's swap `:file` raises E325 — at 49 columns a
        # hit-enter prompt that the next keystrokes have to dismiss (measured
        # 2026-09-28: an Edit whose buffer had vanished, retyped here since
        # the base probe; a fresh Write of a swap-held file hit it too). The
        # buffer is display-only, so there is nothing for a swap to protect,
        # and 'swapfile' is buffer-local: every other buffer keeps its check.
        # Unlike _GOTO_FILE's SwapExists answer, this path reads no file, so
        # there is no ATTENTION dialog to answer, only the check to skip.
        # file_path is read from a handle (_typed_path) into g:vaf_p and goes
        # through fnameescape(), same as _GOTO_FILE: raw on the command line,
        # `#`, `%` and a space rename
        # the buffer onto the wrong name (measured 2026-09-24: a space alone
        # left the buffer unnamed), and the by-number lookup that a later
        # Edit's goto_file does then misses it and opens a duplicate tab.
        # The name is the cwd-relative one Vim would give the file itself
        # (_vim_display_name), not the full path the hook passes. The rename
        # is `silent`: `:file` prints the name it set (`"<name>" [Not
        # edited]`), which is the full path for a file outside the cwd
        # (measured 2026-09-29); errors (E95) still show.
        # Then it READS the file into the buffer and clears it again, on the
        # same command line (see _READ_THEN_CLEAR): the rename left the buffer
        # "not edited", and a plain `:w` over the existing file then failed
        # with E13, so the hand-off cue's ":w releases" was false for every
        # Write interrupted before the relock's `:e!`.
        #
        # The renamed-over buffer may be a plugin scratch screen (start
        # screens set buftype=nofile, and often nomodifiable); the rename
        # inherits both. It is made a regular, modifiable, writable file
        # buffer here, BEFORE the read: a nofile buffer reads no file, and on
        # a nomodifiable one the `%d` fails (E21, silenced), leaving the file
        # it just read on screen until the run's own `:%d` (measured
        # 2026-09-28 on a startify-like buffer). buftype= also keeps the
        # user's :w after an interrupt from failing with E382.
        #
        # In an adopted Vim the rename also claims the option for the follower
        # (see _NOTE_USER_READONLY): whatever readonly the renamed buffer
        # carried belonged to the file it held before, never to this one.
        claim = " | let b:vaf_user_ro = 0 | let b:vaf_ro_ours = 2" if self._is_adopted() else ""
        read = _typed_path(self.pane_id, file_path, "g:vaf_p")
        pane.send_text(
            f":{read} | if g:vaf_p !=# ''{' | tabnew' if in_new_tab else ''}"
            " | setlocal noswapfile"
            f" | silent exe 'file ' . fnameescape({_vim_display_name('g:vaf_p')}){claim}"
            f" | setlocal buftype= modifiable noreadonly | {_READ_THEN_CLEAR}"
            " | endif | unlet! g:vaf_h"
        )
        pane.send_key("Enter")
        _confirm_landing(pane, file_path)
        # An ADOPTED Vim is the user's own editor: its buffer gets swap back
        # on right after the rename, with the ATTENTION message suppressed for
        # that one step (see _SWAP_BACK_ON). A dedicated follower's stays off.
        if self._is_adopted():
            pane.send_text(_SWAP_BACK_ON)
            pane.send_key("Enter")
        # `:filetype detect` must run BEFORE 'paste' is enabled below: it
        # loads the filetype's indent/ftplugin scripts, which can turn
        # cindent/smartindent/indentexpr back on — 'paste' only suppresses
        # whatever was active at the moment it's set, not anything enabled
        # afterwards.
        pane.send_text(":filetype detect")
        pane.send_key("Enter")
        lines = tuple(content.splitlines())

        def run(inner: TmuxPane) -> AnimationResult:
            # Wipe down to a single blank line — Vim can't have zero lines.
            inner.send_text(":%d")
            inner.send_key("Enter")
            return run_lines(
                inner,
                self.window_id,
                lines,
                self._live_pace,
                file_path=file_path,
                on_resume=lambda: self.goto_file(file_path),
                # The `:%d` just above wiped the buffer down to its seed
                # blank, so nothing of the content is on screen yet: the
                # partial is whatever this run has typed and nothing more.
                base_content="",
            )

        return self._with_unlocked(_RELOCK_READONLY_SYNCED, run)

    def resume(
        self,
        pending: PendingApplyEdit | PendingShowFresh,
        *,
        seeded: bool = False,
        reload: bool = True,
    ) -> AnimationResult:
        """Replay a saved remainder. A completed replay relocks with the
        silent `:e!` disk sync (see _with_unlocked) unless `reload` is False.

        reload=False is the pre-edit catch-up (hooks._consume_pending_
        catchup): the remainder belongs to an EARLIER edit, and its end state
        is the base the NEW edit's diff was computed against. By `hook post`
        Claude has already written the new edit, so the `:e!` would load the
        finished file: it flashed on screen, and the base probe that follows
        then saw "differs", so an adopted Vim got a false cue and no
        animation, and a dedicated one wiped and retyped the whole file
        (reproduced 2026-09-25). What the `:e!` protects (grounding the
        buffer's timestamp, so a bare `:w` raises no W11) is still done by
        the new edit's own animation, which runs next and ends in the same
        `:e!`; until then the buffer is in the state every animation is in
        mid-typing. (An adopted Vim whose buffer then turns out not to be the
        base is left alone, with the cue that offers `:e!`.) The des-interrupt
        replay keeps the default: it finishes THIS hook's edit, which is
        exactly what disk holds."""
        # `seeded` is part of the Follower protocol for the nvim backend's
        # explicit seed-provenance; tmux resyncs from disk on relock, so the
        # buffer's exact shape is behaviorally invisible here — ignored.
        del seeded
        if pending.file_path:
            self.goto_file(pending.file_path)
        on_resume = (lambda: self.goto_file(pending.file_path)) if pending.file_path else None
        # The pace-0 catch-up (cmd_pause / _handle_hook_post_edit replaying
        # with pace_seconds=0.0) must stay silent forever — it must never
        # re-read live state and start pacing again mid-catch-up.
        provider = (lambda: 0.0) if pending.pace_seconds == 0.0 else self._live_pace
        # What is on screen when a resume starts IS the pending's own partial:
        # every caller that reaches here on a non-None partial ran
        # rewrite_buffer(file_path, pending.partial) first (hooks'
        # _consume_pending_catchup and _replay_remainder both do), which rebuilt
        # the buffer to exactly that. A None partial stays None, so a re-pause
        # of a legacy pending keeps saying "not recorded" instead of inventing
        # a base — the consumer's live-buffer fallback must stay meaningful.
        if isinstance(pending, PendingApplyEdit):
            return self._with_unlocked(
                _RELOCK_SYNCED if reload else _RELOCK,
                lambda pane: run_ops(
                    pane,
                    self.window_id,
                    pending.ops,
                    provider,
                    file_path=pending.file_path,
                    on_resume=on_resume,
                    base_content=pending.partial,
                ),
            )
        return self._with_unlocked(
            _RELOCK_READONLY_SYNCED if reload else _RELOCK_READONLY,
            lambda pane: run_lines(
                pane,
                self.window_id,
                pending.lines,
                provider,
                continuation=pending.continuation,
                file_path=pending.file_path,
                on_resume=on_resume,
                base_content=pending.partial,
            ),
        )

    def hand_over(self) -> None:
        """Unlock the buffer for direct user editing (interrupt semantics).
        The one place CoC is turned back on — this is the one point a human
        actually gets to type into the buffer themselves and wants real
        completion/hints (see _COC_DISABLE for why it's off otherwise)."""
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(_COC_ENABLE)
        pane.send_key("Enter")
        pane.send_text(":setlocal modifiable nopaste")
        pane.send_key("Enter")
        # A hand-off after rewrite_buffer's unlock (a replay interrupted
        # mid-rebuild), or of a crash-orphaned remainder, gives an adopted
        # Vim's user back their readonly here (see _NOTE_USER_READONLY).
        if self._is_adopted():
            self._send_restore_readonly(pane)

    def goto_line(self, offset: int) -> None:
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(f":{offset}")
        pane.send_key("Enter")

    def stop(self) -> None:
        TmuxPane(pane_id=self.pane_id).kill()

    @classmethod
    def start(cls, target_pane: str) -> TmuxVimFollower:
        pane = TmuxPane.split_from(target_pane, "vim")
        return cls(pane_id=pane.pane_id)
