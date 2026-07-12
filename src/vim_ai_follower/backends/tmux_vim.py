from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, AnimationResult, run_lines, run_ops
from vim_ai_follower.control import PendingApplyEdit, PendingShowFresh
from vim_ai_follower.diff import EditOp
from vim_ai_follower.tmux import TmuxPane


@dataclass(frozen=True)
class TmuxVimFollower:
    """Follower backend that drives a real Vim instance in a tmux pane via
    simulated keystrokes (tmux send-keys)."""

    pane_id: str
    pace_seconds: float = DEFAULT_PACE_SECONDS
    session_id: str = ""

    def is_alive(self) -> bool:
        return TmuxPane(pane_id=self.pane_id).running_command() == "vim"

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
        pane.send_text(":setlocal readonly nomodifiable")
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
        pane.send_text(":setlocal readonly nomodifiable")
        pane.send_key("Enter")

    def _with_unlocked(
        self, relock: str, run: Callable[[TmuxPane], AnimationResult]
    ) -> AnimationResult:
        """Owns the lock protocol shared by every animation entry point.
        'paste' suppresses autoindent/smartindent/cindent for the duration:
        without it, each Enter in insert mode auto-inserts indentation that
        then stacks with the leading whitespace already in our own lines.
        An interrupted animation hands the buffer to the user — it stays
        modifiable. completed/paused both relock (a paused buffer is
        protected, not handed over; run_ops/run_lines always stop on a
        clean boundary rather than a stray mid-typing spot)."""
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(":setlocal modifiable paste")
        pane.send_key("Enter")
        result = run(pane)
        if result.outcome != "interrupted":
            pane.send_text(relock)
            pane.send_key("Enter")
        return result

    def apply_edit(self, file_path: str, ops: list[EditOp]) -> AnimationResult:
        self.goto_file(file_path)
        return self._with_unlocked(
            ":setlocal nomodifiable nopaste",
            lambda pane: run_ops(
                pane, self.session_id, ops, self.pace_seconds, file_path=file_path
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
            return run_lines(inner, self.session_id, lines, self.pace_seconds, file_path=file_path)

        return self._with_unlocked(":setlocal readonly nomodifiable nopaste", run)

    def resume(self, pending: PendingApplyEdit | PendingShowFresh) -> AnimationResult:
        if pending.file_path:
            self.goto_file(pending.file_path)
        if isinstance(pending, PendingApplyEdit):
            return self._with_unlocked(
                ":setlocal nomodifiable nopaste",
                lambda pane: run_ops(
                    pane,
                    self.session_id,
                    pending.ops,
                    pending.pace_seconds,
                    file_path=pending.file_path,
                ),
            )
        return self._with_unlocked(
            ":setlocal readonly nomodifiable nopaste",
            lambda pane: run_lines(
                pane,
                self.session_id,
                pending.lines,
                pending.pace_seconds,
                continuation=pending.continuation,
                file_path=pending.file_path,
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
