from __future__ import annotations

from dataclasses import dataclass

from vim_ai_follower import diff as diff_module
from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, AnimationResult, run_lines, run_ops
from vim_ai_follower.control import PendingApplyEdit, PendingShowFresh
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

    def ensure_showing(self, file_path: str) -> None:
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(f":e {file_path}")
        pane.send_key("Enter")
        # Locked by default so a stray keystroke into this pane can't corrupt
        # the buffer: our own animation is indistinguishable from real
        # typing at the tty level, so it must explicitly unlock around itself.
        pane.send_text(":setlocal readonly nomodifiable")
        pane.send_key("Enter")

    def apply_edit(self, before: str, after: str) -> AnimationResult:
        ops = diff_module.compute_edit_script(before, after)
        pane = TmuxPane(pane_id=self.pane_id)
        # 'paste' suppresses autoindent/smartindent/cindent for the duration:
        # without it, each Enter in insert mode auto-inserts indentation that
        # then stacks with the leading whitespace already in our own lines.
        pane.send_text(":setlocal modifiable paste")
        pane.send_key("Enter")
        result = run_ops(pane, self.session_id, ops, self.pace_seconds)
        # An interrupted animation hands the buffer to the user — it stays
        # modifiable. completed/paused both relock (a paused buffer is
        # protected, not handed over; see run_ops for why paused always
        # lands on a clean op boundary rather than a stray mid-typing spot).
        if result.outcome != "interrupted":
            pane.send_text(":setlocal nomodifiable nopaste")
            pane.send_key("Enter")
        return result

    def show_fresh(self, file_path: str, content: str) -> AnimationResult:
        pane = TmuxPane(pane_id=self.pane_id)
        # Deliberately never `:e file_path` here: that would load the file's
        # real (already-written) content and flash the finished result on
        # screen before the wipe+retype, spoiling the "watch it type" effect.
        # Instead the current buffer is wiped and renamed in place, so the
        # real content is never displayed before we type it back in.
        pane.send_text(f":silent! bwipeout! {file_path}")
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
        pane.send_text(":setlocal modifiable paste")
        pane.send_key("Enter")
        # Wipe down to a single blank line — Vim can't have zero lines.
        pane.send_text(":%d")
        pane.send_key("Enter")
        lines = tuple(content.splitlines())
        result = run_lines(pane, self.session_id, lines, self.pace_seconds)
        if result.outcome != "interrupted":
            pane.send_text(":setlocal readonly nomodifiable nopaste")
            pane.send_key("Enter")
        return result

    def resume(self, pending: PendingApplyEdit | PendingShowFresh) -> AnimationResult:
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(":setlocal modifiable paste")
        pane.send_key("Enter")
        if isinstance(pending, PendingApplyEdit):
            result = run_ops(pane, self.session_id, pending.ops, pending.pace_seconds)
            relock = ":setlocal nomodifiable nopaste"
        else:
            result = run_lines(
                pane,
                self.session_id,
                pending.lines,
                pending.pace_seconds,
                continuation=pending.continuation,
            )
            relock = ":setlocal readonly nomodifiable nopaste"
        if result.outcome != "interrupted":
            pane.send_text(relock)
            pane.send_key("Enter")
        return result

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
