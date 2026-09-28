from __future__ import annotations

import logging
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
# But a readonly the USER set (`:view`, `vim -R`, in an adopted Vim) is theirs
# to keep, and both the unlock and the completion relock's `:e!` (a reload
# resets 'readonly') took it away, for good on the interrupted path. So the
# unlock records in `b:vaf_user_ro` whether the readonly it clears was the
# user's, and _RESTORE_READONLY puts it back on every exit: after the relock,
# at an interrupt, and in hand_over. Paused time never leaves the run, so it
# needs nothing. The record lives in the buffer, not in Python, so the paths
# that never reach a restore (the hook killed mid-pause, an exception mid-run)
# keep it: the unlock never overwrites an existing record, and the next
# completion, interrupt or hand_over restores it.
#
# Whose readonly it is decides everything: the follower sets 'readonly' itself
# (ensure_showing's lock, show_fresh's relock), and restoring THAT at an
# interrupt would turn the hand-off's ":w releases" into E45. The follower's
# own readonly locks stamp `b:changedtick` into `b:vaf_ro_tick`; every reload
# moves the tick (measured 2026-09-28, like any change), so a readonly whose
# stamp is stale was set by someone else since: a user `:view` of a file the
# follower had locked counts as the user's. The one case it misreads is a bare
# `:setlocal readonly` on a buffer the follower had already locked readonly,
# which changes nothing the user can see either. show_fresh records "not the
# user's" at its rename: the buffer it renames belonged to some other file.
#
# Applied on every follower, dedicated too: a dedicated follower's buffers are
# only ever readonly by its own locks (stamped), so the restore only acts on a
# readonly its user set by hand, and it only ever ADDS 'readonly', so the
# dedicated relocks are untouched.
_STAMP_OUR_READONLY = "let b:vaf_ro_tick = b:changedtick"
_LOCK_READONLY = ":setlocal readonly nomodifiable | " + _STAMP_OUR_READONLY
_UNLOCK_FOR_ANIMATION = (
    ":if !exists('b:vaf_user_ro')"
    " | let b:vaf_user_ro = &readonly && get(b:, 'vaf_ro_tick', -1) != b:changedtick"
    " | endif | setlocal noreadonly modifiable paste"
)
_RESTORE_READONLY = ":if get(b:, 'vaf_user_ro') | setlocal readonly | endif | unlet! b:vaf_user_ro"
_RELOCK = ":setlocal nomodifiable nopaste"
_RELOCK_READONLY = ":setlocal readonly nomodifiable nopaste | " + _STAMP_OUR_READONLY

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
# The number is resolved the way _WIPE_BUFFER does it — the path lives in a
# Vim string literal and the buffer list is walked comparing `:p` names —
# because `bufnr()` and `:buffer {name}` take a PATTERN. A window already
# showing it is focused (`win_gotoid`, any tab); a loaded buffer with no
# window gets a new tab via `:tab sbuffer {nr}`, which does not re-read a
# loaded buffer; the current buffer is left alone. All three stay inside
# the try/SwapExists guard: `:tab sbuffer` on a listed-but-unloaded buffer
# does read the file, and can raise the ATTENTION dialog like the drop.
# The `g:` variables are unlet in `finally` (inert if a cut-off line leaves
# them, like _WIPE_BUFFER's).
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
# `g:vaf_n` = the number of the buffer named `g:vaf_p`, or -1 (see above).
_FIND_BUFFER = (
    ":let g:vaf_p = {file}"
    " | let g:vaf_n = get(filter(range(1, bufnr('$')), 'bufexists(v:val)"
    " && fnamemodify(bufname(v:val), '':p'') ==# fnamemodify(g:vaf_p, '':p'')'), 0, -1)"
)
_GOTO_FILE = (
    _FIND_BUFFER + f" | {_SWAP_ANSWER_OPEN}"
    " | try"
    " | if g:vaf_n < 0 | exe 'tab drop ' . fnameescape(g:vaf_p)"
    " | elseif g:vaf_n != bufnr('%')"
    " | exe win_gotoid(get(win_findbuf(g:vaf_n), 0)) ? '' : 'tab sbuffer ' . g:vaf_n"
    " | endif"
    r" | catch /^Vim\%((\a\+)\)\=:E37:/"
    f" | finally | {_SWAP_ANSWER_CLOSE}"
    " | unlet! g:vaf_p g:vaf_n | endtry"
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
    ":try | "
    + _FIND_BUFFER.removeprefix(":")
    + " | let g:vaf_r = writefile(getbufline(g:vaf_n, 1, '$') + [{nonce}], {probe})"
    " | catch | finally | unlet! g:vaf_p g:vaf_n g:vaf_r | endtry"
)
# How long probe_buffer waits for Vim's answer before calling it "unknown"
# (the hook's safe side: a retype, or in an adopted Vim, leaving it alone).
# Measured 2026-09-24 against a real tmux+vim at load 4-6: a
# median of ~50 ms and a worst of ~100 ms per probe, for 50- and 2000-line
# buffers alike, so this only runs out when Vim is stuck or the machine is
# drowning, and then retyping is the right call anyway.
_PROBE_TIMEOUT_SECONDS = 2.0
_PROBE_POLL_SECONDS = 0.01

logger = logging.getLogger("vim_ai_follower")


def _probe_path(pane_id: str) -> Path:
    """Where Vim writes probe_buffer's answer for this pane."""
    return cache.CACHE_DIR / f"probe-{pane_id.lstrip('%')}.txt"


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
# So the path never reaches a pattern at all. It goes into a Vim string
# literal (see _vim_string), and the buffer list is walked comparing FULL
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
    ":let g:vaf_wipe_name = fnamemodify({file}, ':p')"
    " | let g:vaf_wipe_nr = get(filter(range(1, bufnr('$')),"
    ' \'bufexists(v:val) && bufname(v:val) !=# ""'
    ' && fnamemodify(bufname(v:val), ":p") ==# g:vaf_wipe_name\'), 0, -1)'
    " | if g:vaf_wipe_nr > 0 | exe 'silent! bwipeout! ' . g:vaf_wipe_nr | endif"
    " | unlet! g:vaf_wipe_name g:vaf_wipe_nr"
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

        file_path reaches Vim as a string literal (_vim_string, total) run
        through Vim's own `fnameescape()`, never raw: as a bare `tab drop`
        argument `#`/`%` expanded to the alternate/current file, `$NAME` to
        an environment variable, a space split it into two files and a glob
        opened a matching sibling (all measured 2026-09-22). `:exe` keeps
        the E37 catch intact — the error still reads `Vim(drop):E37:`."""
        pane = TmuxPane(pane_id=self.pane_id)
        self._normal_mode(pane)
        pane.send_text(_GOTO_FILE.format(file=_vim_string(file_path)))
        pane.send_key("Enter")

    def reload_and_relock(self, file_path: str) -> None:
        """Des-interrupt: discard the user's unsaved typing by reloading the
        file Claude wrote, then resume following it."""
        self.goto_file(file_path)
        pane = TmuxPane(pane_id=self.pane_id)
        # Not a bare `:e!`: on a buffer with swap on whose file another Vim
        # holds, it put up the whole ATTENTION dialog (see _SYNC_FROM_DISK).
        pane.send_text(_RELOAD_DISCARDING)
        pane.send_key("Enter")
        pane.send_text(_LOCK_READONLY)
        pane.send_key("Enter")

    def reload_from_disk(self, file_path: str) -> None:
        """Ground the buffer on the file as it is on disk, discarding what it
        holds, and lock it. Only for a buffer holding nothing but follower
        text: a catch-up that just completed (hooks._animate_edit). Without
        it that buffer stays modified while disk moved on, and the next
        `:checktime` raises the blocking W12 dialog."""
        self.goto_file(file_path)
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(_RELOAD_DISCARDING)
        pane.send_key("Enter")
        pane.send_text(_LOCK_READONLY)
        pane.send_key("Enter")

    def rewrite_buffer(self, file_path: str, content: str) -> AnimationResult:
        """Instantly (pace 0) rebuild the buffer to `content` — the
        des-interrupt replay needs the buffer back at the interrupt-point
        state, discarding the user's unsaved typing, before the remainder
        resumes at live pace. Leaves the buffer unlocked: resume() runs
        immediately after and owns the relock."""
        self.goto_file(file_path)
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(_UNLOCK_FOR_ANIMATION)
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
        pane.send_text(_WIPE_BUFFER.format(file=_vim_string(file_path)))
        pane.send_key("Enter")

    def ensure_showing(self, file_path: str) -> None:
        """The Read/binary entry point: show the file as it is ON DISK. It
        is the one navigation that re-reads a loaded buffer (clean only, see
        _RELOAD_IF_CLEAN); goto_file itself never does, because it also runs
        before every animation."""
        self.goto_file(file_path)
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(_RELOAD_IF_CLEAN)
        pane.send_key("Enter")
        # Locked by default so a stray keystroke into this pane can't corrupt
        # the buffer: our own animation is indistinguishable from real
        # typing at the tty level, so it must explicitly unlock around itself.
        pane.send_text(_LOCK_READONLY)
        pane.send_key("Enter")

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
                file=_vim_string(file_path),
                nonce=_vim_string(nonce),
                probe=_vim_string(str(probe)),
            )
        )
        pane.send_key("Enter")
        deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
        while (lines := _read_probe(probe, nonce)) is None:
            if time.monotonic() >= deadline:
                logger.warning(
                    "no answer from the follower's Vim about %s within %.1fs",
                    file_path,
                    _PROBE_TIMEOUT_SECONDS,
                )
                return "unknown"
            time.sleep(_PROBE_POLL_SECONDS)
        probe.unlink(missing_ok=True)
        return classify_buffer(lines, content)

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
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(_COC_DISABLE)
        pane.send_key("Enter")
        pane.send_text(_UNLOCK_FOR_ANIMATION)
        pane.send_key("Enter")
        result = run(pane)
        if result.outcome != "interrupted":
            pane.send_text(relock)
            pane.send_key("Enter")
        # After the relock, whose `:e!` resets 'readonly' (and on an interrupt,
        # which leaves the buffer to the user without one).
        pane.send_text(_RESTORE_READONLY)
        pane.send_key("Enter")
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
        pane.send_text(_WIPE_BUFFER.format(file=_vim_string(file_path)))
        pane.send_key("Enter")
        if in_new_tab:
            pane.send_text(":tabnew")
            pane.send_key("Enter")
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
        pane.send_text(":setlocal noswapfile")
        pane.send_key("Enter")
        # file_path goes through _vim_string + fnameescape(), same as
        # _GOTO_FILE: raw on the command line, `#`, `%` and a space rename
        # the buffer onto the wrong name (measured 2026-09-24: a space alone
        # left the buffer unnamed), and the by-number lookup that a later
        # Edit's goto_file does then misses it and opens a duplicate tab.
        # The rename also records "not the user's readonly" for the unlock
        # (see _UNLOCK_FOR_ANIMATION): whatever readonly the renamed buffer
        # carries belonged to the file it held before.
        #
        # Then it READS the file into the buffer and clears it again, on the
        # same command line (see _READ_THEN_CLEAR): the rename left the buffer
        # "not edited", and a plain `:w` over the existing file then failed
        # with E13, so the hand-off cue's ":w releases" was false for every
        # Write interrupted before the relock's `:e!`.
        #
        # The renamed-over buffer may be a plugin scratch screen (start
        # screens set buftype=nofile); the rename inherits that and the
        # user's :w after an interrupt would fail with E382. It is made a
        # regular file buffer here, before the read, which is a file read
        # only on a regular buffer.
        pane.send_text(
            f":exe 'file ' . fnameescape({_vim_string(file_path)})"
            " | let b:vaf_user_ro = 0 | setlocal buftype= | " + _READ_THEN_CLEAR
        )
        pane.send_key("Enter")
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
        # mid-rebuild), or of a crash-orphaned remainder, gives back the
        # user's readonly here (see _UNLOCK_FOR_ANIMATION).
        pane.send_text(_RESTORE_READONLY)
        pane.send_key("Enter")

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
