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
_GOTO_FILE = r":try | tab drop {file} | catch /^Vim\%((\a\+)\)\=:E37:/ | endtry"


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
        missing. Wrapped in _GOTO_FILE's E37 guard so a modified target
        can't leave a blocking hit-enter prompt in the pane — see that
        constant for why the bang, `:silent!` and 'hidden' are all wrong.

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
        pane = TmuxPane(pane_id=self.pane_id)
        self.goto_file(file_path)
        # bwipeout! of a buffer that is its tab's only window already
        # closes that tab; never follow it with :tabclose — focus lands on
        # a neighboring tab and the "safety" close eats an innocent one
        # (live eviction bug, 2026-07-15). On the last remaining tab the
        # wipe just leaves an empty buffer, which is fine.
        pane.send_text(f":silent! bwipeout! {file_path}")
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
        pane.send_text(f":silent! bwipeout! {file_path}")
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
