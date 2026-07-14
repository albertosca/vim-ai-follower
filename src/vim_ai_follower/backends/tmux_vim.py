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


@dataclass(frozen=True)
class TmuxVimFollower:
    """Follower backend that drives a real Vim instance in a tmux pane via
    simulated keystrokes (tmux send-keys)."""

    pane_id: str
    pace_seconds: float = DEFAULT_PACE_SECONDS
    session_id: str = ""

    def is_alive(self) -> bool:
        return TmuxPane(pane_id=self.pane_id).running_command() == "vim"

    def _live_pace(self) -> float:
        """Re-read the current speed from FollowerState so a running
        animation reacts to Ctrl+a +/- at its next line boundary, instead
        of only on the animation started after the toggle. Falls back to
        the pace this follower was constructed with when there's no
        session to read state for (or no state was ever written)."""
        if not self.session_id:
            return self.pace_seconds
        state = FollowerState.read(self.session_id)
        if state is None:
            return self.pace_seconds
        return config.pace_seconds_for(state.speed)

    def _normal_mode(self, pane: TmuxPane) -> None:
        # Ctrl-\ Ctrl-N returns to Normal mode from ANY mode (insert, visual,
        # command-line) without side effects — never trust where the user
        # left the follower Vim.
        pane.send_key("C-\\")
        pane.send_key("C-n")

    def goto_file(self, file_path: str) -> None:
        """The defensive preamble: land on the tab showing file_path (by
        name, immune to the user closing/reordering tabs), opening one if
        missing."""
        pane = TmuxPane(pane_id=self.pane_id)
        self._normal_mode(pane)
        pane.send_text(f":tab drop {file_path}")
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

    def close_tab(self, file_path: str) -> None:
        pane = TmuxPane(pane_id=self.pane_id)
        self.goto_file(file_path)
        # silent!: closing the last remaining tab fails (E784) — acceptable,
        # the buffer just stays.
        pane.send_text(f":silent! bwipeout! {file_path}")
        pane.send_key("Enter")
        pane.send_text(":silent! tabclose")
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
        pane.send_text(_UNLOCK_FOR_ANIMATION)
        pane.send_key("Enter")
        result = run(pane)
        if result.outcome != "interrupted":
            pane.send_text(relock)
            pane.send_key("Enter")
        return result

    def apply_edit(self, file_path: str, ops: list[EditOp]) -> AnimationResult:
        self.goto_file(file_path)
        return self._with_unlocked(
            _RELOCK_SYNCED,
            lambda pane: run_ops(
                pane,
                self.session_id,
                ops,
                self._live_pace,
                file_path=file_path,
                on_resume=lambda: self.goto_file(file_path),
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
                self.session_id,
                lines,
                self._live_pace,
                file_path=file_path,
                on_resume=lambda: self.goto_file(file_path),
            )

        return self._with_unlocked(_RELOCK_READONLY_SYNCED, run)

    def resume(self, pending: PendingApplyEdit | PendingShowFresh) -> AnimationResult:
        if pending.file_path:
            self.goto_file(pending.file_path)
        on_resume = (lambda: self.goto_file(pending.file_path)) if pending.file_path else None
        # The pace-0 catch-up (cmd_pause / _handle_hook_post_edit replaying
        # with pace_seconds=0.0) must stay silent forever — it must never
        # re-read live state and start pacing again mid-catch-up.
        provider = (lambda: 0.0) if pending.pace_seconds == 0.0 else self._live_pace
        if isinstance(pending, PendingApplyEdit):
            return self._with_unlocked(
                _RELOCK_SYNCED,
                lambda pane: run_ops(
                    pane,
                    self.session_id,
                    pending.ops,
                    provider,
                    file_path=pending.file_path,
                    on_resume=on_resume,
                ),
            )
        return self._with_unlocked(
            _RELOCK_READONLY_SYNCED,
            lambda pane: run_lines(
                pane,
                self.session_id,
                pending.lines,
                provider,
                continuation=pending.continuation,
                file_path=pending.file_path,
                on_resume=on_resume,
            ),
        )

    def hand_over(self) -> None:
        """Unlock the buffer for direct user editing (interrupt semantics)."""
        pane = TmuxPane(pane_id=self.pane_id)
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
