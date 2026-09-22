from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from vim_ai_follower import config
from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, AnimationResult, run_lines, run_ops
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
# read-only lock, an in-place edit is merely made unmodifiable.
_LOCK_READONLY = ":setlocal readonly nomodifiable"
_UNLOCK_FOR_ANIMATION = ":setlocal modifiable paste"
_RELOCK_SYNCED = ":silent! e! | setlocal nomodifiable nopaste"
_RELOCK_READONLY_SYNCED = ":silent! e! | setlocal readonly nomodifiable nopaste"

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
_SWAP_GROUP = "vim_ai_follower_swap"
_GOTO_FILE = (
    f':exe "augroup {_SWAP_GROUP}"'
    " | exe \"autocmd SwapExists * ++once let v:swapchoice = 'e'\""
    ' | exe "augroup END"'
    r" | try | tab drop {file} | catch /^Vim\%((\a\+)\)\=:E37:/"
    f' | finally | exe "autocmd! {_SWAP_GROUP}" | exe "augroup! {_SWAP_GROUP}" | endtry'
)


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
        missing. Wrapped in _GOTO_FILE's guards so neither a modified
        target (E37) nor a swap file on the target (the ATTENTION dialog,
        answered `(E)dit anyway`) can leave a blocking prompt in the pane
        — see that constant for why the bang, `:silent!`, 'hidden',
        'shortmess' and 'noswapfile' are all wrong.

        This is the single navigation preamble every other method calls,
        so both guards cover show_fresh, ensure_showing, apply_edit,
        reload_and_relock, rewrite_buffer, resume and close_tab at once.

        file_path is interpolated raw, exactly as it always has been: it
        stays in `tab drop`'s own file argument, so its (pre-existing)
        handling of spaces, `%`, `#` and wildcards is unchanged by the
        wrapper."""
        pane = TmuxPane(pane_id=self.pane_id)
        self._normal_mode(pane)
        pane.send_text(_GOTO_FILE.format(file=file_path))
        pane.send_key("Enter")

    def reload_and_relock(self, file_path: str) -> None:
        """Des-interrupt: discard the user's unsaved typing by reloading the
        file Claude wrote, then resume following it."""
        self.goto_file(file_path)
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(":e!")
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
        self.goto_file(file_path)
        # Locked by default so a stray keystroke into this pane can't corrupt
        # the buffer: our own animation is indistinguishable from real
        # typing at the tty level, so it must explicitly unlock around itself.
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(_LOCK_READONLY)
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
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(_COC_DISABLE)
        pane.send_key("Enter")
        pane.send_text(_UNLOCK_FOR_ANIMATION)
        pane.send_key("Enter")
        result = run(pane)
        if result.outcome != "interrupted":
            pane.send_text(relock)
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
        pane.send_text(f":file {file_path}")
        pane.send_key("Enter")
        # `:filetype detect` must run BEFORE 'paste' is enabled below: it
        # loads the filetype's indent/ftplugin scripts, which can turn
        # cindent/smartindent/indentexpr back on — 'paste' only suppresses
        # whatever was active at the moment it's set, not anything enabled
        # afterwards.
        pane.send_text(":filetype detect")
        pane.send_key("Enter")
        # The renamed-over buffer may be a plugin scratch screen (start
        # screens set buftype=nofile); the rename inherits that and the
        # user's :w after an interrupt would fail with E382. Make it a
        # regular file buffer.
        pane.send_text(":setlocal buftype=")
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
        self, pending: PendingApplyEdit | PendingShowFresh, *, seeded: bool = False
    ) -> AnimationResult:
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
                _RELOCK_SYNCED,
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
            _RELOCK_READONLY_SYNCED,
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
