from __future__ import annotations

import contextlib
import hashlib
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
from vim_ai_follower.backends import BufferProbe, NavigationFailed, classify_buffer
from vim_ai_follower.control import PendingApplyEdit, PendingShowFresh
from vim_ai_follower.diff import EditOp
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane

# This backend drives Vim by TYPING Ex commands (`tmux send-keys`), and Vim
# echoes every typed command on its command line before it runs it. The logic
# below used to be typed whole on every call: the navigation line alone grew to
# ~1,500 characters, a 12-30-row block of Vim script flashing over the
# follower's bottom on every Edit and Read (found on the demo, 2026-10-05). So
# the logic is Vim script, defined ONCE per Vim as functions (_VIM_SCRIPT,
# sourced from a file in the cache directory), and a typed line only calls
# them: one or two screen rows at the follower pane's 49 columns (_call_line).
#
# Every public entry point that may be the first thing a Vim hears from this
# hook sends _define_line first: it sources the script only when the function
# is missing. That is what heals a Vim that lost the functions — the user's
# `:delfunction`, or an adopted Vim restarted in the same shell, which tmux
# cannot tell apart from the old one (same pane process) — on the very next
# call, with no state on the Python side. Each call line is guarded by the same
# `exists()`, so a Vim the source did not reach (another HOME, see
# _home_relative) runs nothing, raises no E117 prompt, and simply does not
# answer: the navigation then fails safe (_await_landing).
#
# The script is legacy Vim script, run with 'cpoptions' at its Vim default.
# Every function is `abort`, so an error stops it exactly where an error used
# to stop the rest of a typed line; `try`/`finally` still run their cleanup.
# The helpers are script-local (`s:`), so the user's namespace gets ONE
# function, the dispatcher, named after a hash of the script
# (`VafFollower_<6 hex>`): a Vim holding an older version's functions sources
# the new one instead of calling it with the wrong arguments, and the new
# script deletes the older dispatchers it finds. The only globals the lines
# share between them are the landing's (`g:vaf_p`, `g:vaf_k`, `g:vaf_landed`,
# see _VIM_LANDING), unlet by the line that answers.
#
# The script names the cache directory itself (`s:dir`), in full: it is a file,
# never echoed, so the handle, probe and answer files the functions read and
# write need no `~` and no HOME question. Only the define line types a path.

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
# _VIM_GOTO), the unlock has to say it (measured 2026-09-23).
#
# But in an ADOPTED Vim a readonly the USER set (`:view`, `vim -R`) is theirs
# to keep, and both the unlock and the completion relock's `:e!` (a reload
# resets 'readonly') took it away, for good on the interrupted path. So an
# adopted Vim's lines record the user's readonly in `b:vaf_user_ro` and
# s:restore_readonly puts it back on every exit: after the relock, at an
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
# s:note_user_readonly runs before every step of the follower's that changes
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
_ADOPTED_LOCK_READONLY = _LOCK_READONLY + " | let b:vaf_ro_ours = 1"
_OUR_READONLY_CLAIM = " | let b:vaf_ro_ours = 1"
# The restore also leaves its answer for user_readonly (the hand-off cue) in
# `readonly-<pane>.txt`: whether the buffer is now readonly by the user's
# setting. Written here, by the line the follower sends anyway, so the cue
# costs no extra keystrokes into a Vim the user is about to type into (a
# separate probe line at the hand-off interleaved with the user's first keys;
# measured 2026-09-28 with a test's keys, which garbled both command lines).
# Inside try/catch: no failure may leave a prompt.
_VIM_READONLY = r"""
function! s:note_user_readonly() abort
  if !get(b:, 'vaf_ro_ours')
    let b:vaf_user_ro = &readonly
  endif
endfunction

function! s:unlock() abort
  call s:note_user_readonly()
  setlocal noreadonly modifiable paste
  let b:vaf_ro_ours = 2
endfunction

function! s:restore_readonly(pane) abort
  if get(b:, 'vaf_user_ro')
    setlocal readonly
    let b:vaf_ro_ours = 0
  elseif get(b:, 'vaf_ro_ours') == 2
    let b:vaf_ro_ours = 0
  endif
  unlet! b:vaf_user_ro
  let answer = &readonly && !get(b:, 'vaf_ro_ours') ? '1' : '0'
  try
    call writefile([answer], s:dir . 'readonly-' . a:pane . '.txt')
  catch
  endtry
endfunction
"""

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

# goto_file's navigation (s:goto), wrapped so a dirty target can't stall the
# pane.
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
#     E37 still fires — and a restore that is not in `finally` never runs
#     after the error, leaking `hidden` ON into what, in adopt mode, is the
#     user's own Vim (measured: &hidden left at 1 in all three dirty
#     scenarios).
#
# The catch pattern is the documented `Vim(cmd):E37:` form and is narrow
# in both directions (measured against E17/E212/E325/E370, all of which
# still surface exactly as they do today).
#
# The SECOND stall the same navigation has to survive is Vim's swap-file
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
# So the hook is registered and torn down around the one read that needs it
# (s:swap_answer_open / s:swap_answer_close):
#
#   - It lives in its own augroup, created by an `augroup` command first.
#     `:autocmd {group} ...` does NOT create a missing group: it fails
#     with `E216` and leaves its own hit-enter prompt (measured), which is
#     the very failure being fixed.
#   - There is deliberately no bare `:autocmd!` anywhere. It would only
#     ever apply to `vim_ai_follower_swap`, but if the preceding `augroup`
#     ever failed it would run in the DEFAULT group and wipe every
#     autocommand the user has. Nothing needs it: `finally` clears the
#     group on every pass.
#   - The teardown is in `finally`, so it runs on the E37 path and on an
#     uncaught error too (both measured: no residue, and a non-E37 error
#     still reaches the user).
#   - `++once` bounds the one residual risk — the call being cut off
#     mid-flight, before `finally` — to a single auto-answered dialog
#     instead of the policy persisting in the user's Vim.
#
# Scoping is by TIME, not by pattern: the hook exists only for the
# duration of this drop, so a user `:e` of a swapped file afterwards
# still gets the normal dialog (measured). Matching on the path instead
# would mean escaping it into an autocmd pattern — a quoting hazard
# avoided entirely.
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
# The number is resolved the way s:wipe does it — the path is read from a
# handle file (s:read) and the buffer list is walked comparing full names
# (s:find) — because `bufnr()` and `:buffer {name}` take a PATTERN. A window
# already showing it is focused (`win_gotoid`, any tab); a loaded buffer
# with no window gets a new tab via `:tab sbuffer {nr}`, which does not
# re-read a loaded buffer; the current buffer is left alone. All three stay
# inside the try/SwapExists guard: `:tab sbuffer` on a listed-but-unloaded
# buffer does read the file, and can raise the ATTENTION dialog like the
# drop.
#
# The drop and `:tab sbuffer` are `silent` (never `silent!`, see above: errors
# and the E37 exception still come through, measured by
# tests/test_integration_goto_file_e37.py and the swap tests). Both print the
# file-info message (`"<name>" 3L, 18B`) when they read a file, and for a
# file outside the cwd that name is the full path — shown on the command line
# the path handle exists to keep it off (measured 2026-09-29,
# tests/test_integration_cmdline_paths.py).
_VIM_SWAP_ANSWER = r"""
function! s:swap_answer_open() abort
  augroup vim_ai_follower_swap
    autocmd SwapExists * ++once let v:swapchoice = 'e'
  augroup END
endfunction

function! s:swap_answer_close() abort
  autocmd! vim_ai_follower_swap
  augroup! vim_ai_follower_swap
endfunction
"""

# The completion relocks' `:e!` disk sync (see the lock protocol at the top),
# carrying the same scoped (E)dit-anyway answer. `:edit` re-runs the swap
# search, so on a buffer with swap ON (an adopted Vim's own buffers, the ones
# show_fresh gives swap back in an adopted Vim, a dedicated buffer a Read
# opened through `:tab drop`) whose file another Vim holds, the bare
# `:silent! e!` stalled INVISIBLY at the hidden ATTENTION dialog: its own
# `setlocal` never ran and the follower's next keys answered the dialog
# (measured 2026-09-28 at 49 columns, tests/test_integration_swap_reload.py).
# `silent!` stays on the edit itself: the relock never showed an error before.
# The relocks that set the follower's own readonly in an adopted Vim also
# mark the follower's claim on it (`claim`, see s:note_user_readonly).
_VIM_RELOCK = r"""
function! s:relock(readonly, claim) abort
  call s:swap_answer_open()
  try
    silent! edit!
  finally
    call s:swap_answer_close()
  endtry
  if a:readonly
    setlocal readonly nomodifiable nopaste
  else
    setlocal nomodifiable nopaste
  endif
  if a:claim
    let b:vaf_ro_ours = 1
  endif
endfunction
"""

# Names, lookups and the landing verdict.
#
# s:display: the name a file `path` gets the way Vim names a file opened by
# RELATIVE path: relative to the working directory when it is under it, full
# otherwise. It is the argument s:rename's `:file` and s:goto's `:tab drop`
# hand to fnameescape(). Why: Vim keeps a name it was given in full as typed,
# and shortens it only when a `:cd` happens. Measured 2026-09-29 on Vim 9.2 at
# 49 columns, cwd /private/tmp/vafn: `:file /private/tmp/vafn/sub/a.py` showed
# the tabline as `/p/t/v/s/a.py`, the full path in the statusline and
# `<te/tmp/vafn/sub/a.py" 1L, 6B written` for `:w`; `:file sub/a.py` shows
# `s/a.py`, `sub/a.py` and `"sub/a.py" 1L, 6B written`, exactly as `:e
# sub/a.py` does. The buffer's FULL name is the same either way, so the `:p`
# lookups (s:find) still find it, and Vim re-bases the short name itself on
# any later `:cd`. `:.` compares against getcwd(), which is the PHYSICAL
# directory even when Vim was started from, or `:cd` to, a symlinked spelling
# of it (measured, macOS /tmp -> /private/tmp); the hook hands the backend a
# realpath (hooks._file_path), so the two line up without resolving anything
# here. The short name is kept only when it ROUND-TRIPS: its `:p` must be
# exactly the `:p` of the full path, the name the lookups start from.
# Otherwise the full path is the name, and the lookups match it verbatim. Two
# measured ways the short form fails to come back (Vim 9.2, macOS):
#   - A wrong-CASE path. `:.` shortens case-INSENSITIVELY on macOS: with cwd
#     `…/Proj`, `…/proj/sub/a.py` shortens to `sub/a.py`, whose `:p` is
#     `…/Proj/sub/a.py` — not the hook's `…/proj/sub/a.py` (the hook's
#     realpath keeps the case it was given). Every lookup missed: the probe
#     said "absent", show_fresh's pre-wipe missed so `:file` hit E95 and
#     stacked unnamed tabs, max_tabs eviction never fired, `:w` gave E32.
#   - A relative name starting with `~` (a directory literally called `~x`
#     under the cwd): `:p` reads it as user x's home, not the file.
#
# s:resolved: the `resolve()`d full name of `name`, or its plain full name
# when resolve() fails: a symlink loop raises E655 (measured 2026-09-29), and
# inside a lookup over every buffer one such name aborted the whole lookup,
# so while a looping buffer existed every navigation failed (review of
# a790652). Each name gets its own try, which is why s:find is a `:for` loop,
# never filter().
#
# s:same_file: whether `name` is the file `target` (already resolved) names,
# compared the way Vim itself tells files apart, not by spelling: resolve()d
# full names (a buffer opened through a file symlink, like a dotfiles
# checkout's ~/.vimrc, while the hook passes the realpath), and
# case-insensitively exactly where the FILESYSTEM is (`folds`: 1 or 0, decided
# by Python, see _case_folds). Never `&fileignorecase`: macOS Vim has it on by
# default, and on a case-sensitive volume, where Readme.md and README.md are
# two files, it made the follower act on the wrong one (review, 2026-09-29,
# measured on a Case-sensitive APFS image: an Edit typed into the sibling's
# buffer, a retype wiped it, an adopted Read locked the user's modified
# sibling). `index(…, ic)` is the one comparison that takes it as a value.
# Measured 2026-09-29: comparing `:p` names alone said "absent" for a symlink
# and a case variant, and `:tab drop` then landed on the existing buffer anyway
# (Vim matches files by identity), under a name no check expected.
#
# s:find: the number of the buffer holding `path`, or -1. A number with no
# buffer has the name '', whose `:p` is the working directory (trailing slash
# included), which no file path equals.
#
# s:read: the path a handle file holds (see _path_handle), deleting the
# handle; '' when it cannot be read (one swept under a stalled Vim), and every
# function then acts on nothing. `filereadable()` first, never a try around
# the read: measured 2026-09-29 (Vim 9.2), readfile()'s E484 is not a clean
# exception on a typed line, and a missing handle must cost nothing.
# Byte-exact: `readfile(…, 'b')` joined on "\n" gives back exactly the bytes
# Python wrote, a newline in the path included (binary mode keeps a CR and
# adds no item for a missing final newline).
#
# The landing (s:acting_start, s:land_if_target, s:landed). A call that must
# ACT on the target (goto, show_fresh's rename) records, by buffer NUMBER,
# where it landed (`g:vaf_landed`, set only when, after its navigation, the
# current buffer IS the target by s:same_file: an existing buffer for the
# same file under another name counts, a sibling Vim switched to by name never
# does) next to its call's token (`g:vaf_k`, set first, after an `unlet!` of
# the rest, so a value left by an earlier, garbled call can never pass), and
# is followed by a call to s:landed, which writes the verdict and the token to
# `landed-<pane>.txt`: "landed" only when the current buffer is that one;
# "unread" when the path handle could not be read; "elsewhere" otherwise. The
# Python side waits for it (_await_landing) and sends nothing more without it,
# so no lock, unlock, keystroke or relock can land on whatever buffer happened
# to be current (review, 2026-09-29: a failed goto let a Read lock the user's
# own modified buffer, and an Edit's relock `:e!` discard the user's unsaved
# text). The token is the call's handle name (`p<pane>-<6 hex>`), which also
# names the pane whose answer file it is.
#
# s:landed is a line of its own, after an Escape pair, never the acting call's
# tail. Measured 2026-09-29 (tests/test_integration_edit_no_reload.py's
# no-window case: one tab left, so `:tab sbuffer` opens a second): the goto
# line, then 21-27 screen rows at 49 columns, left Vim at a prompt that showed
# nothing new. As the line's tail, the answer never came, even after 90 s; as
# a separate line, its first screen row (49 characters) was swallowed and the
# rest ran as `ufname('%')…` (E492). The Escape pair (_normal_mode's)
# dismisses the prompt, and the check then runs whole.
_VIM_LANDING = r"""
function! s:display(path) abort
  let short = fnamemodify(a:path, ':.')
  return fnamemodify(short, ':p') ==# fnamemodify(a:path, ':p') ? short : a:path
endfunction

function! s:resolved(name) abort
  try
    return resolve(fnamemodify(a:name, ':p'))
  catch
    return fnamemodify(a:name, ':p')
  endtry
endfunction

function! s:same_file(target, name, folds) abort
  return index([a:target], s:resolved(a:name), 0, a:folds) == 0
endfunction

function! s:find(path, folds) abort
  let target = s:resolved(a:path)
  for number in range(1, bufnr('$'))
    if s:same_file(target, bufname(number), a:folds)
      return number
    endif
  endfor
  return -1
endfunction

function! s:read(handle) abort
  let handle = s:dir . a:handle
  let path = filereadable(handle) ? join(readfile(handle, 'b'), "\n") : ''
  call delete(handle)
  return path
endfunction

function! s:pane(token) abort
  return matchstr(a:token, '^p\zs[0-9]\+')
endfunction

function! s:acting_start(token) abort
  unlet! g:vaf_p g:vaf_landed
  let g:vaf_k = a:token
  let g:vaf_p = s:read(a:token)
endfunction

function! s:land_if_target(folds) abort
  if s:same_file(s:resolved(g:vaf_p), bufname('%'), a:folds)
    let g:vaf_landed = bufnr('%')
  endif
endfunction

function! s:landed(token) abort
  if get(g:, 'vaf_p', '') ==# ''
    let verdict = 'unread'
  elseif get(g:, 'vaf_k', '') ==# a:token && get(g:, 'vaf_landed', -1) == bufnr('%')
    let verdict = 'landed'
  else
    let verdict = 'elsewhere'
  endif
  try
    call writefile([verdict, a:token], s:dir . 'landed-' . s:pane(a:token) . '.txt')
  catch
  endtry
  unlet! g:vaf_p g:vaf_k g:vaf_landed
endfunction
"""

# s:goto: see the comment above _VIM_SWAP_ANSWER. 'fileignorecase' is off
# for the navigation, restored in `finally`: Vim then tells files apart by
# identity (inode) as the filesystem does — measured 2026-09-29, with it on
# (the macOS default) `:tab drop README.md` landed on the Readme.md buffer
# and `:file README.md` took Readme.md's buffer's name, both on a
# case-sensitive volume; with it off both are right there, and on a
# case-insensitive volume Vim still finds a case variant by inode. E37 comes
# from the drop's trailing `:rewind`, after it landed, and falls through to
# the landing check; any other error aborts the function there, so the
# landing is never recorded.
_VIM_GOTO = r"""
function! s:goto(token, folds) abort
  call s:acting_start(a:token)
  if g:vaf_p ==# ''
    return
  endif
  let number = s:find(g:vaf_p, a:folds)
  call s:swap_answer_open()
  let fileignorecase = &fileignorecase
  let &fileignorecase = 0
  try
    if number < 0
      exe 'silent tab drop ' . fnameescape(s:display(g:vaf_p))
    elseif number != bufnr('%')
      exe win_gotoid(get(win_findbuf(number), 0)) ? '' : 'silent tab sbuffer ' . number
    endif
  catch /^Vim\%((\a\+)\)\=:E37:/
  finally
    let &fileignorecase = fileignorecase
    call s:swap_answer_close()
  endtry
  call s:land_if_target(a:folds)
endfunction
"""

# ensure_showing's disk re-read (s:reload with discard 0), for the Read/binary
# navigation where showing the file AS IT IS ON DISK is the whole point.
# goto_file no longer re-reads a loaded buffer (it runs before animations,
# where that is the bug above), so a file open and clean in the follower that
# something other than Claude's Edit rewrote — a formatter run through Bash,
# `sed -i`, a `git checkout` — would otherwise be shown stale, and locked
# read-only.
#
#   - `!&modified`: a dirty buffer is typed-but-unsaved content (an interrupt
#     hand-off, a killed hook) and is never discarded here; that also makes
#     E37 unreachable, so no catch is needed.
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
#
# reload_from_disk's and the des-interrupt's re-read (discard 1): the same
# without the clean check, for a buffer that holds only follower text the
# hook has decided to discard (see hooks._animate_edit, after a completed
# catch-up), or the user's typing a des-interrupt discards. In an adopted
# Vim (`adopted`) the user's readonly is noted first, since the reload resets
# it (see s:note_user_readonly).
_VIM_RELOAD = r"""
function! s:reload(discard, adopted) abort
  if a:adopted
    call s:note_user_readonly()
  endif
  call s:swap_answer_open()
  try
    if a:discard
      silent edit!
    elseif !&modified
      silent edit
    endif
  finally
    call s:swap_answer_close()
  endtry
endfunction
"""

# show_fresh's swap re-enable for an ADOPTED Vim (s:swap_back_on, Alberto's
# ruling, 2026-09-28): without a swap, a crash loses what the user types into
# the buffer and a second editor opening the file gets no ATTENTION. Turning
# swap on runs the swap check the rename's opt-out avoided; measured
# 2026-09-28 at 49 columns with an owner Vim holding `.swp`, a plain
# `:setlocal swapfile` left the ATTENTION block at `-- More --`, a scoped
# SwapExists answer does not fire for an option toggle, and `shortmess+=A`
# saved and restored in `finally` was silent, created `.swo`, and a second Vim
# opening the file still got its SwapExists. The nvim backend runs the same
# toggle over RPC.
#
# Prompt-proof when the toggle fails. A swap file Vim cannot create (E303:
# no writable 'directory') makes Vim set need_wait_return itself, so the line
# ended at "Press ENTER" at 49 columns and swallowed the `:filetype detect`
# and the keys after it. Measured 2026-09-28 with a forced E303: `silent!` on
# the setlocal, or a `catch`, hides the message but NOT the prompt; the
# trailing `redraw` (an intentional redraw clears need_wait_return) is what
# removes it, and with a writable 'directory' the swap is still created.
# The swap then stays unmade, which is the safe side.
_VIM_SWAP_BACK_ON = r"""
function! s:swap_back_on() abort
  let shortmess = &shortmess
  set shortmess+=A
  try
    silent! setlocal swapfile
  finally
    let &shortmess = shortmess
  endtry
  redraw
endfunction
"""

# show_fresh's rename-in-place (s:rename). Deliberately never `:e file_path`:
# that would load the file's real (already-written) content and flash the
# finished result on screen before the wipe+retype, spoiling the "watch it
# type" effect. Instead the current buffer (or a new tab's) is renamed onto
# the path, and nothing after it runs until Python's landing check: a failed
# read opens no stray tab and does not turn swap off for the user's current
# buffer.
#
#   - Swap off BEFORE naming: renaming a swap-enabled buffer runs the swap
#     check for the new name, and when a live Vim holds the file's swap
#     `:file` raises E325 — at 49 columns a hit-enter prompt that the next
#     keystrokes have to dismiss (measured 2026-09-28: an Edit whose buffer
#     had vanished, retyped here since the base probe; a fresh Write of a
#     swap-held file hit it too). The buffer is display-only, so there is
#     nothing for a swap to protect, and 'swapfile' is buffer-local: every
#     other buffer keeps its check. Unlike s:goto's SwapExists answer, this
#     path reads no file, so there is no ATTENTION dialog to answer, only the
#     check to skip.
#   - The path goes through fnameescape(), same as s:goto: raw, `#`, `%` and
#     a space rename the buffer onto the wrong name (measured 2026-09-24: a
#     space alone left the buffer unnamed), and the by-number lookup that a
#     later Edit's goto_file does then misses it and opens a duplicate tab.
#     The name is the cwd-relative one Vim would give the file itself
#     (s:display). The rename is `silent`: `:file` prints the name it set
#     (`"<name>" [Not edited]`), which is the full path for a file outside
#     the cwd (measured 2026-09-29); errors (E95) still show. It runs with
#     'fileignorecase' off: with it on, `:file README.md` on a
#     case-sensitive volume took the name of the Readme.md buffer (measured
#     2026-09-29), and the landing then confirms identity.
#   - Everything after the rename that changes the current buffer (the
#     readonly claim, the buftype/modifiable reset, the read-and-clear) runs
#     only on that confirmed landing (`exists('g:vaf_landed')`, unlet by this
#     call's s:acting_start): a user autocommand can move the cursor on the
#     rename (`BufFilePost … tabfirst`), and the tail then wiped the USER's
#     buffer, unsaved text and all, before the landing check could stop
#     anything (review, 2026-09-29).
#   - The renamed-over buffer may be a plugin scratch screen (start screens
#     set buftype=nofile, and often nomodifiable); the rename inherits both.
#     It is made a regular, modifiable, writable file buffer here, BEFORE the
#     read: a nofile buffer reads no file, and on a nomodifiable one the `%d`
#     fails (E21, silenced), leaving the file it just read on screen until
#     the run's own `:%d` (measured 2026-09-28 on a startify-like buffer).
#     buftype= also keeps the user's :w after an interrupt from failing with
#     E382.
#   - In an adopted Vim (`adopted`) the rename also claims the option for the
#     follower (see s:note_user_readonly): whatever readonly the renamed
#     buffer carried belonged to the file it held before, never to this one.
#
# Then it READS the file into the buffer and clears it again, in the same
# call. Renaming a buffer onto a path (`:file`, and nvim's buf_set_name)
# marks it "not edited", and Vim refuses a plain `:w` of a not-edited buffer
# over an existing file with `E13: File exists (add ! to override)`; only a
# read or a write clears the mark (measured 2026-09-28, Vim and nvim alike).
# Found recording the demo: the hand-off cue said ":w releases" and only
# `:w!` worked. The read also gives the buffer the file's timestamp, so a
# plain `:w` after something else rewrote the file asks "changed since reading
# it" instead of overwriting silently (a not-edited buffer skipped that check).
#
#   - Vim does not redraw in the middle of a call, so the file's content is
#     never on screen (the rename still exists so that nothing else ever is).
#   - `noautocmd`: no BufRead autocommands, so plugins (CoC) attach no
#     earlier than they did (the relock's `:e!`), and none can force a redraw
#     mid-call. `:filetype detect` runs afterwards anyway.
#   - swap is already off, so the read raises no ATTENTION; an adopted Vim's
#     swap goes back on after (s:swap_back_on).
#   - `silent!`: a file that cannot be read must not leave a prompt at 49
#     columns; the buffer then just stays not edited, as before.
#   - `%d _` into the black-hole register, leaving the user's registers alone.
_VIM_RENAME = r"""
function! s:rename(token, folds, in_new_tab, adopted) abort
  call s:acting_start(a:token)
  if g:vaf_p ==# ''
    return
  endif
  if a:in_new_tab
    tabnew
  endif
  setlocal noswapfile
  let fileignorecase = &fileignorecase
  let &fileignorecase = 0
  try
    silent exe 'file ' . fnameescape(s:display(g:vaf_p))
  finally
    let &fileignorecase = fileignorecase
  endtry
  call s:land_if_target(a:folds)
  if exists('g:vaf_landed')
    if a:adopted
      let b:vaf_user_ro = 0
      let b:vaf_ro_ours = 2
    endif
    setlocal buftype= modifiable noreadonly
    noautocmd silent! edit!
    silent! %d _
  endif
endfunction
"""

# Wipe the buffer holding a given file (s:wipe), resolved by NUMBER rather
# than by name. This is the eviction primitive close_tab uses and the
# pre-wipe show_fresh does before it renames a buffer onto the same path.
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
# So the path never reaches a pattern at all: it is read from a handle file
# into a string, and the buffer list is walked (s:find: same file, not same
# spelling). Only an exact hit is wiped, and a miss — or a handle that could
# not be read, which gives '' — wipes nothing. That is load-bearing, not
# merely tidy: a bare `:bwipeout!` with no number would wipe the CURRENT
# buffer, and `fnamemodify('', ':p')` is the working directory, the name of a
# buffer opened on `.`.
#
# Never follow this with `:tabclose`. Wiping a buffer that is its tab's only
# window already closes that tab, and the "safety" close then lands on
# whichever neighbour received focus and eats an innocent one (live eviction
# bug, 2026-07-15).
_VIM_WIPE = r"""
function! s:wipe(token, folds) abort
  let path = s:read(a:token)
  if path ==# ''
    return
  endif
  let number = s:find(path, a:folds)
  if number > 0
    exe 'silent! bwipeout! ' . number
  endif
endfunction
"""

# probe_buffer's read-back (s:probe): the one place this keystroke-driven
# backend asks Vim about a buffer's content. Vim writes the lines of the
# buffer the handle names into `probe-<pane>.txt`, which Python polls for. It
# looks the buffer up by NUMBER exactly like s:goto (never a name pattern) and
# never navigates: `:tab sbuffer` on an unloaded buffer is the very disk read
# the question exists to avoid.
#
#   - getbufline() of an unloaded buffer (listed, but its tab was closed under
#     'nohidden') and of a missing one (-1) is an empty list, while a loaded
#     buffer always has at least one line — so "no lines" already means
#     "absent", with no status field to keep in sync (classify_buffer);
#   - the token closes the dump, so a half-written file, or one left by an
#     older probe that timed out and landed late, is never read as this
#     probe's answer;
#   - the lines themselves, not a `sha256()` of them: that needs +cryptv, and
#     an E117 at 49 columns is a hit-enter prompt that would eat the
#     animation's keystrokes;
#   - everything inside try with an empty catch-all: no failure may leave a
#     prompt in the pane. Measured 2026-09-25 on a real Vim at 49 columns: an
#     unwritable probe directory raised `E482: Can't create file <path>`,
#     whose long path wrapped into "Press ENTER or type command to continue" —
#     and in an adopted Vim nothing is sent after the probe to dismiss it.
#     Swallowed, the error just means no answer, which the poll turns into
#     "unknown" at the timeout;
#   - a handle that could not be read writes no answer at all, so it is
#     "unknown", never "absent".
_VIM_PROBE = r"""
function! s:probe(token, folds) abort
  try
    let path = s:read(a:token)
    if path !=# ''
      let lines = getbufline(s:find(path, a:folds), 1, '$')
      call writefile(lines + [a:token], s:dir . 'probe-' . s:pane(a:token) . '.txt')
    endif
  catch
  endtry
endfunction
"""

# The whole script. `@DIR@` is the cache directory (a Vim string literal,
# trailing slash included) and `@NAME@` the dispatcher's name, both filled in
# by _vim_functions. The dispatcher is defined LAST, so `exists()` of it is
# true only once everything before it was read. The loop after it deletes the
# dispatchers of other versions: a `VafFollower_<6 hex>` only when Vim says it
# was set from the follower's own script for that hash, `vaf-<6 hex>.vim`
# (`:verbose function`'s "Last set from <path>" line, whose words are
# translated but whose path is not; measured 2026-10-05 in C and pt_BR). A
# user function of the same name shape, set anywhere else, stays (review of
# 1e558d0: `VafFollower_123abc()` was deleted). No variable records the
# names, so nothing new is left in the user's namespace.
_VIM_SCRIPT = (
    "\" vim-ai-follower's tmux backend: written to its cache directory and\n"
    '" sourced once per Vim (see backends/tmux_vim.py). Do not edit.\n'
    "scriptencoding utf-8\n"
    "let s:cpo_save = &cpo\n"
    "set cpo&vim\n"
    "let s:dir = @DIR@\n"
    + _VIM_READONLY
    + _VIM_SWAP_ANSWER
    + _VIM_RELOCK
    + _VIM_LANDING
    + _VIM_GOTO
    + _VIM_RELOAD
    + _VIM_SWAP_BACK_ON
    + _VIM_RENAME
    + _VIM_WIPE
    + _VIM_PROBE
    + r"""
function! @NAME@(op, ...) abort
  return call('s:' . a:op, a:000)
endfunction

for s:name in getcompletion('VafFollower_', 'function')
  let s:name = matchstr(s:name, '^VafFollower_\x\{6}\ze(')
  if s:name ==# '' || s:name ==# '@NAME@'
    continue
  endif
  let s:origin = get(split(execute('verbose function ' . s:name), "\n"), 1, '')
  if s:origin =~# '[/\\]vaf-' . s:name[12:] . '\.vim\>'
    exe 'delfunction ' . s:name
  endif
endfor
unlet! s:name s:origin
let &cpo = s:cpo_save
unlet s:cpo_save
"""
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
# (_await_landing). Far longer than a probe's: measured 2026-09-29, when the
# goto line was ~1500 typed characters, a follower Vim running a 15 ms timer
# (tests/test_integration_edit_no_reload.py's observer) was still echoing it
# after 2 s at load 3. The lines are short calls now, but running out means
# the edit is skipped, so this stays sized for a slow Vim; the one case that
# always waits it out is a navigation that cannot land (a stale HOME answer,
# so the functions were never sourced), and that one heals itself
# (_distrust_home).
_LANDING_TIMEOUT_SECONDS = 10.0

logger = logging.getLogger("vim_ai_follower")


def _probe_path(pane_id: str) -> Path:
    """Where Vim writes probe_buffer's answer for this pane (s:probe)."""
    return cache.CACHE_DIR / f"probe-{pane_id.lstrip('%')}.txt"


def _readonly_answer_path(pane_id: str) -> Path:
    """Where s:restore_readonly leaves user_readonly's answer for this pane."""
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


def _vim_functions() -> tuple[str, str]:
    """(dispatcher name, script text) of _VIM_SCRIPT for the current cache
    directory. The name carries a hash of the text, so any change to the
    script, or another cache directory, is another name (see the comment at
    the top)."""
    text = _VIM_SCRIPT.replace("@DIR@", _vim_string(f"{cache.CACHE_DIR}/"))
    name = "VafFollower_" + hashlib.sha256(text.encode()).hexdigest()[:6]
    return name, text.replace("@NAME@", name)


def _script_file(name: str, text: str) -> Path:
    """The script's file in the cache directory, written when missing or not
    exactly `text`: through a temporary file and a rename, so a Vim never
    sources a half-written one."""
    directory = cache.CACHE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"vaf-{name.removeprefix('VafFollower_')}.vim"
    data = text.encode()
    try:
        if path.read_bytes() == data:
            return path
    except OSError:
        pass
    partial = directory / f".{path.name}.{secrets.token_hex(4)}"
    partial.write_bytes(data)
    partial.replace(path)
    return path


def _call_line(op: str, *arguments: str | int) -> str:
    """The typed line that runs s:<op>(arguments) through the dispatcher, and
    does nothing — no E117, no prompt — in a Vim that does not have it.

    No spaces around the bars: every typed line must fit two screen rows at
    the follower pane's 49 columns, and that is 97 characters with the `:`
    (measured 2026-10-05, Vim 9.2: `:` + 97 characters puts the cursor on a
    third row). Show_fresh's rename is the longest call, and its handle
    carries the pane id, which a long-lived tmux server takes past four
    digits."""
    name, _text = _vim_functions()
    rendered = ",".join(
        _vim_string(argument) if isinstance(argument, str) else str(argument)
        for argument in (op, *arguments)
    )
    return f":if exists('*{name}')|call {name}({rendered})|endif"


def _define_line(pane_id: str) -> str:
    """The typed line that sources the script when this Vim lacks its
    dispatcher (see the comment at the top). `sil! so` keeps it short and
    silent: a file Vim cannot read (a Vim on another HOME, see
    _home_relative) is no error, no prompt; the calls then do nothing and the
    navigation fails safe. A `~/…` name or a path of safe characters is typed
    as is (`:source` expands `~` itself, and 'wildignore' does not apply,
    measured 2026-10-05); any other path goes through fnameescape()."""
    name, text = _vim_functions()
    path = _script_file(name, text)
    target = _home_relative(pane_id, path)
    if target is None and _EXPAND_SAFE.fullmatch(str(path)):
        target = str(path)
    if target is None:
        source = f"exe 'sil! so ' . fnameescape({_vim_string(str(path))})"
    else:
        source = f"sil! so {target}"
    return f":if !exists('*{name}')|{source}|endif"


# How long a path handle (see _path_handle) is kept before a later call sweeps
# it, whichever pane it was for. Vim deletes a handle in the call that reads
# it, well under a second after the send even at load 15; this only bounds
# the ones no Vim ever read (a cut-off line, a Vim without the functions, a
# pane that died). Sweeping one early fails safe, never to another file: a
# missing handle reads as '' (s:read) and the call then acts on nothing, and
# a call that had to act says so by not answering "landed" (s:landed).
_PATH_HANDLE_MAX_AGE_SECONDS = 600.0

# The characters a cache path may have to be typed bare: as `~/…` (the HOME
# question's expand(), and the define line's `:source`, which would also
# expand a wildcard `*?[{`, `$NAME`, a backtick command or a second `~` in
# it), and as an absolute path on the define line. The real one is
# `.cache/claude-vim-follower`. The question's `1` (nosuf) matters as much:
# without it 'wildignore' applies, and measured 2026-09-29 (Vim 9.2) a Vim
# with `wildignore=*/.cache/*` expands a cache path to ''.
_EXPAND_SAFE = re.compile(r"[A-Za-z0-9._/-]+")

# The one-time question to a pane's Vim: create {marker} (a `~/…` path under
# the hook's cache directory) through Vim's own `~`. It lands in the hook's
# CACHE_DIR only when that Vim's HOME is the hook's HOME. `silent!` on the one
# command: a failure (another HOME without that directory, E482) shows
# nothing and leaves no prompt.
_HOME_CHECK = ":silent! call writefile([], expand({marker}, 1))"


def _vim_shares_home(pane_id: str, cache_relative: str) -> bool:
    """Whether `~` in this pane's Vim is the hook's HOME, so the script file
    can be typed as `~/{cache_relative}/…`.

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
    another HOME (`HOME=… vim`). Then its define line sources nothing, the
    calls do nothing, the navigation does not land and nothing more is sent
    (s:landed), and the unanswered question drops the stale "yes"
    (_distrust_home) so the next call asks this Vim again. Asking every Vim
    would take a round trip per call; tmux names no foreground pid to key
    on."""
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


def _home_relative(pane_id: str, path: Path) -> str | None:
    """`path`, a file in the cache directory, as `~/.cache/…` when this
    pane's Vim shares the hook's HOME and the name is safe to type bare;
    None otherwise. Typed lines are echoed on Vim's command line, and HOME is
    often a long absolute directory of its own (in the demo, one under
    /tmp)."""
    try:
        cache_relative = Path(os.path.realpath(cache.CACHE_DIR)).relative_to(
            os.path.realpath(Path.home())
        )
    except ValueError:
        return None
    if not _EXPAND_SAFE.fullmatch(str(cache_relative)) or not _EXPAND_SAFE.fullmatch(path.name):
        return None
    if not _vim_shares_home(pane_id, str(cache_relative)):
        return None
    return f"~/{cache_relative}/{path.name}"


def _path_handle(pane_id: str, file_path: str) -> Path:
    """A handle file in the cache directory holding `file_path`, for a call
    to read (s:read) instead of the path being typed.

    Why: this backend TYPES its Ex commands, and Vim echoes a typed command on
    its command line before running it. With the path spelled in the line,
    every edit and navigation flashed the target's absolute path in the
    follower's footer, up to five screen rows of it at 49 columns (found on a
    demo recording, 2026-09-29; tests/test_integration_cmdline_paths.py logs
    every command-line change). The line now shows only the handle's name,
    `p<pane>-<6 hex>`, which is also the call's token (s:landed).

    - One handle PER CALL (a random suffix), never a fixed per-pane file: Vim
      runs a line some time after the send returns, so a fixed file could be
      rewritten by the next call first — close_tab's eviction followed by
      show_fresh's pre-wipe would then both wipe the NEW file, and the evicted
      one would stay. A handle is only ever read by the call that names it,
      which deletes it right after, and is created exclusively (`xb`), so two
      calls never share one.
    - Byte-exact: the file holds `os.fsencode(file_path)` with no trailing
      newline (see s:read). A NUL cannot occur in a path."""
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
        return handle


# A path handle's name (_path_handle): `p<pane number>-<6 hex>`.
_HANDLE_NAME = re.compile(r"p[0-9]+-[0-9a-f]{6}")


def _goto_answer_path(pane_id: str) -> Path:
    """Where a call that must land on its target confirms it did (s:landed)."""
    return cache.CACHE_DIR / f"landed-{pane_id.lstrip('%')}.txt"


def _confirm_landing(pane: TmuxPane, file_path: str, handle: Path) -> None:
    """Ask Vim whether the acting call just sent (whose token is `handle`'s
    name, the file it read its path from) landed on file_path, and raise
    NavigationFailed unless it says so."""
    answer = _goto_answer_path(pane.pane_id)
    answer.parent.mkdir(parents=True, exist_ok=True)
    answer.unlink(missing_ok=True)
    # Clears a prompt the acting call can leave (see s:landed).
    pane.send_key("Escape")
    pane.send_key("Escape")
    pane.send_text(_call_line("landed", handle.name))
    pane.send_key("Enter")
    _await_landing(pane.pane_id, answer, handle.name, file_path, handle)


def _distrust_home(pane_id: str) -> bool:
    """Forget a "yes" to the HOME question (see _vim_shares_home) after a
    question to the pane's Vim went unanswered: if its `~` is no longer the
    hook's (an adopted pane's shell restarted Vim with another HOME), the
    `~/…` script name it is typed misses, and only a new question finds out.
    A recorded "no" is kept: asking again costs a full wait for nothing."""
    record = cache.CACHE_DIR / f"home-{pane_id.lstrip('%')}"
    try:
        _pid, marker = record.read_text().split()
    except (OSError, ValueError):
        return False
    if not (cache.CACHE_DIR / marker).exists():
        return False
    record.unlink(missing_ok=True)
    return True


# Why a navigation did not land, per s:landed's verdict (None: no answer at
# all).
_NOT_LANDED = {
    "unread": "its path handle could not be read",
    "elsewhere": "it landed on another buffer",
    None: "no answer",
}


def _await_answer(path: Path, nonce: str, timeout: float) -> list[str] | None:
    """What Vim wrote to `path` closed by `nonce` (_read_probe), waited for
    up to `timeout` seconds; None when it never came."""
    deadline = time.monotonic() + timeout
    while (lines := _read_probe(path, nonce)) is None:
        if time.monotonic() >= deadline:
            return None
        time.sleep(_PROBE_POLL_SECONDS)
    return lines


def _await_landing(pane_id: str, answer: Path, nonce: str, file_path: str, handle: Path) -> None:
    """Raise NavigationFailed unless Vim confirmed (s:landed) that the
    current buffer is the one the acting call landed on.

    Only a failure that points at the HOME question distrusts its answer: no
    answer at all while the path handle is still unread (Vim deletes it as it
    reads it), which is what a Vim whose `~` is not the hook's leaves — the
    script was never sourced there, so nothing ran. An answer, or a handle
    Vim did read, means the functions reach it: a landing elsewhere, a Vim
    busy in the command-line window, or one too slow for the wait skip the
    edit and keep the answer."""
    lines = _await_answer(answer, nonce, _LANDING_TIMEOUT_SECONDS)
    answer.unlink(missing_ok=True)
    if lines == ["landed"]:
        return
    verdict = lines[0] if lines else None
    reason = _NOT_LANDED.get(verdict, "an unexpected answer")
    if lines is None and handle.exists() and _distrust_home(pane_id):
        reason += "; asking about its HOME again"
    handle.unlink(missing_ok=True)
    raise NavigationFailed(f"the follower's Vim did not land on {file_path} ({reason})")


def _case_folds(file_path: str) -> int:
    """1 when the filesystem holding file_path ignores case in its name (the
    macOS default volume), 0 otherwise: the `folds` the lookups compare
    buffer names with (s:same_file). Asked of the filesystem itself, as
    whether the path with every letter's case swapped is the same file; a
    path with no letter, or one that cannot be checked (missing, unreadable),
    is 0, the side that never takes one file for another. Per path, not per
    Vim: a case-sensitive volume can be mounted anywhere."""
    swapped = file_path.swapcase()
    if swapped == file_path:
        return 0
    try:
        return int(Path(file_path).samefile(swapped))
    except (OSError, ValueError):
        return 0


@dataclass(frozen=True)
class _Relock:
    """How a completed animation relocks its buffer (see the lock protocol at
    the top): `synced` re-reads the file first (s:relock), `readonly` sets the
    follower's readonly lock, which an adopted Vim's relock also claims (see
    s:note_user_readonly)."""

    synced: bool
    readonly: bool

    def line(self, adopted: bool) -> str:
        claim = adopted and self.readonly
        if self.synced:
            return _call_line("relock", int(self.readonly), int(claim))
        plain = _RELOCK_READONLY if self.readonly else _RELOCK
        return plain + (_OUR_READONLY_CLAIM if claim else "")


_RELOCK_SYNCED = _Relock(synced=True, readonly=False)
_RELOCK_READONLY_SYNCED = _Relock(synced=True, readonly=True)
_RELOCK_PLAIN = _Relock(synced=False, readonly=False)
_RELOCK_READONLY_PLAIN = _Relock(synced=False, readonly=True)


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
        return _call_line("unlock") if self._is_adopted() else _UNLOCK_FOR_ANIMATION

    def _define(self, pane: TmuxPane) -> None:
        """Make sure this Vim has the functions the call lines run (see the
        comment at the top of this module)."""
        pane.send_text(_define_line(self.pane_id))
        pane.send_key("Enter")

    def _send_reload_and_lock(self, pane: TmuxPane, *, discard: bool) -> None:
        """A disk re-read (s:reload, which resets 'readonly'), then the
        follower's readonly lock. In an adopted Vim the user's readonly is
        noted first and the lock claims the option (see
        s:note_user_readonly)."""
        adopted = self._is_adopted()
        pane.send_text(_call_line("reload", int(discard), int(adopted)))
        pane.send_key("Enter")
        pane.send_text(_ADOPTED_LOCK_READONLY if adopted else _LOCK_READONLY)
        pane.send_key("Enter")

    def goto_file(self, file_path: str) -> None:
        """The defensive preamble: land on the tab showing file_path (by
        name, immune to the user closing/reordering tabs), opening one if
        missing. A buffer that already holds the file is switched to by
        number and NEVER re-read from disk — only a file no buffer holds
        goes through `:tab drop` (see _VIM_SWAP_ANSWER for the reload bug
        that rule fixes). Wrapped in s:goto's guards so neither a modified
        target (E37) nor a swap file on the target (the ATTENTION dialog,
        answered `(E)dit anyway`) can leave a blocking prompt in the pane
        — see that constant for why the bang, `:silent!`, 'hidden',
        'shortmess' and 'noswapfile' are all wrong.

        This is the single navigation preamble every other method calls,
        so both guards cover show_fresh, ensure_showing, apply_edit,
        reload_and_relock, rewrite_buffer, resume and close_tab at once.

        file_path reaches Vim as a string read from a handle file
        (_path_handle: never spelled on the command line, which Vim echoes)
        run through Vim's own `fnameescape()`, never raw: as a bare `tab drop`
        argument `#`/`%` expanded to the alternate/current file, `$NAME` to
        an environment variable, a space split it into two files and a glob
        opened a matching sibling (all measured 2026-09-22). `:exe` keeps
        the E37 catch intact — the error still reads `Vim(drop):E37:`.

        Waits for Vim to confirm it landed (s:landed) and raises
        NavigationFailed otherwise, so no caller sends a lock, an unlock, an
        animation or a relock onto whatever buffer is current instead."""
        pane = TmuxPane(pane_id=self.pane_id)
        self._normal_mode(pane)
        self._define(pane)
        handle = _path_handle(self.pane_id, file_path)
        pane.send_text(_call_line("goto", handle.name, _case_folds(file_path)))
        pane.send_key("Enter")
        _confirm_landing(pane, file_path, handle)

    def reload_and_relock(self, file_path: str) -> None:
        """Des-interrupt: discard the user's unsaved typing by reloading the
        file Claude wrote, then resume following it."""
        self.goto_file(file_path)
        pane = TmuxPane(pane_id=self.pane_id)
        # Not a bare `:e!`: on a buffer with swap on whose file another Vim
        # holds, it put up the whole ATTENTION dialog (see _VIM_RELOCK).
        self._send_reload_and_lock(pane, discard=True)

    def reload_from_disk(self, file_path: str) -> None:
        """Ground the buffer on the file as it is on disk, discarding what it
        holds, and lock it. Only for a buffer holding nothing but follower
        text: a catch-up that just completed (hooks._animate_edit). Without
        it that buffer stays modified while disk moved on, and the next
        `:checktime` raises the blocking W12 dialog."""
        self.goto_file(file_path)
        pane = TmuxPane(pane_id=self.pane_id)
        self._send_reload_and_lock(pane, discard=True)

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
        load-bearing — s:wipe resolves a buffer NUMBER, and wiping by
        number closes the right tab from wherever the cursor happens to be,
        the same thing the nvim backend measured for its own close_tab — and
        it was actively harmful: `:tab drop` on a path Vim does not already
        hold OPENS a tab for it, so an eviction whose wipe then missed
        ADDED a tab instead of removing one. With the name-pattern wipe that
        preceded it, that pair is how the tab count climbed past max_tabs
        while the follower's own bookkeeping stayed pinned at the limit."""
        pane = TmuxPane(pane_id=self.pane_id)
        self._normal_mode(pane)
        self._define(pane)
        self._send_wipe(pane, file_path)

    def _send_wipe(self, pane: TmuxPane, file_path: str) -> None:
        handle = _path_handle(self.pane_id, file_path)
        pane.send_text(_call_line("wipe", handle.name, _case_folds(file_path)))
        pane.send_key("Enter")

    def ensure_showing(self, file_path: str) -> None:
        """The Read/binary entry point: show the file as it is ON DISK. It
        is the one navigation that re-reads a loaded buffer (clean only, see
        s:reload); goto_file itself never does, because it also runs
        before every animation."""
        self.goto_file(file_path)
        pane = TmuxPane(pane_id=self.pane_id)
        # Locked by default so a stray keystroke into this pane can't corrupt
        # the buffer: our own animation is indistinguishable from real
        # typing at the tty level, so it must explicitly unlock around itself.
        self._send_reload_and_lock(pane, discard=False)

    def probe_buffer(self, file_path: str, content: str) -> BufferProbe:
        """What Vim's buffer for file_path holds relative to `content`, the
        base an edit script is about to be typed onto (see BufferProbe). The
        probe is a round-trip (s:probe): Vim dumps the buffer to a file
        and this polls for it. No answer within _PROBE_TIMEOUT_SECONDS is
        "unknown"."""
        probe = _probe_path(self.pane_id)
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.unlink(missing_ok=True)
        pane = TmuxPane(pane_id=self.pane_id)
        self._normal_mode(pane)
        self._define(pane)
        # The handle's name is also the answer's nonce (see s:probe).
        handle = _path_handle(self.pane_id, file_path)
        pane.send_text(_call_line("probe", handle.name, _case_folds(file_path)))
        pane.send_key("Enter")
        lines = _await_answer(probe, handle.name, _PROBE_TIMEOUT_SECONDS)
        if lines is None:
            logger.warning(
                "no answer from the follower's Vim about %s within %.1fs%s",
                file_path,
                _PROBE_TIMEOUT_SECONDS,
                "; asking about its HOME again" if _distrust_home(self.pane_id) else "",
            )
            return "unknown"
        probe.unlink(missing_ok=True)
        return classify_buffer(lines, content)

    def user_readonly(self, file_path: str) -> bool:
        """See Follower.user_readonly. A dedicated follower never asks: its
        readonly is never the user's. In an adopted Vim, the answer the last
        readonly restore left (s:restore_readonly), waited for up to
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
                logger.warning(
                    "no readonly answer from the follower's Vim about %s%s",
                    file_path,
                    "; asking about its HOME again" if _distrust_home(self.pane_id) else "",
                )
                return False
            time.sleep(_PROBE_POLL_SECONDS)

    def _send_restore_readonly(self, pane: TmuxPane) -> None:
        answer = _readonly_answer_path(self.pane_id)
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.unlink(missing_ok=True)
        pane.send_text(_call_line("restore_readonly", int(self.pane_id.lstrip("%"))))
        pane.send_key("Enter")

    def _with_unlocked(
        self, relock: _Relock, run: Callable[[TmuxPane], AnimationResult]
    ) -> AnimationResult:
        """Owns the lock protocol shared by every animation entry point.
        'paste' suppresses autoindent/smartindent/cindent for the duration:
        without it, each Enter in insert mode auto-inserts indentation that
        then stacks with the leading whitespace already in our own lines.
        An interrupted animation hands the buffer to the user — it stays
        modifiable. A pause never reaches here: it loops inside run_ops/
        run_lines and only returns once the run has actually completed or
        been interrupted, so 'relock' only ever fires on a genuinely
        completed outcome. Callers own the relock, whose synced form
        starts with a silent `:e!` disk sync (see apply_edit/show_fresh/
        resume) — the buffer's name matches the file Claude just wrote, so
        the reload is visually a no-op, but it grounds the buffer's
        timestamp and clears the W11 staleness that an unsynced retype
        would otherwise leave behind."""
        adopted = self._is_adopted()
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(_COC_DISABLE)
        pane.send_key("Enter")
        pane.send_text(_call_line("unlock") if adopted else _UNLOCK_FOR_ANIMATION)
        pane.send_key("Enter")
        result = run(pane)
        if result.outcome != "interrupted":
            pane.send_text(relock.line(adopted))
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
        # The current buffer (or a new tab's) is wiped and renamed in place,
        # so the file's real, already-written content is never displayed
        # before it is typed back in (see _VIM_RENAME).
        self._normal_mode(pane)
        self._define(pane)
        # Same by-number wipe as close_tab, and for the same reason: a
        # name-pattern miss here leaves the old buffer alive, and the
        # `:file` below then hangs a SECOND buffer off the same path.
        self._send_wipe(pane, file_path)
        # The rename, run only when the path read succeeded, and nothing
        # after it is sent until Vim confirms it landed (_confirm_landing).
        handle = _path_handle(self.pane_id, file_path)
        adopted = self._is_adopted()
        pane.send_text(
            _call_line("rename", handle.name, _case_folds(file_path), int(in_new_tab), int(adopted))
        )
        pane.send_key("Enter")
        _confirm_landing(pane, file_path, handle)
        # An ADOPTED Vim is the user's own editor: its buffer gets swap back
        # on right after the rename, with the ATTENTION message suppressed for
        # that one step (see _VIM_SWAP_BACK_ON). A dedicated follower's stays
        # off.
        if adopted:
            pane.send_text(_call_line("swap_back_on"))
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
        else:
            self._define(TmuxPane(pane_id=self.pane_id))
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
                _RELOCK_SYNCED if reload else _RELOCK_PLAIN,
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
            _RELOCK_READONLY_SYNCED if reload else _RELOCK_READONLY_PLAIN,
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
        # Vim's user back their readonly here (see s:note_user_readonly).
        if self._is_adopted():
            self._define(pane)
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
