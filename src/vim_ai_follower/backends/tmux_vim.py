from __future__ import annotations

from dataclasses import dataclass

from vim_ai_follower import diff as diff_module
from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, pace_for, render_keystrokes
from vim_ai_follower.animate import apply as apply_keystrokes
from vim_ai_follower.tmux import TmuxPane


@dataclass(frozen=True)
class TmuxVimFollower:
    """Follower backend that drives a real Vim instance in a tmux pane via
    simulated keystrokes (tmux send-keys)."""

    pane_id: str
    pace_seconds: float = DEFAULT_PACE_SECONDS

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

    def apply_edit(self, before: str, after: str) -> None:
        ops = diff_module.compute_edit_script(before, after)
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(":setlocal modifiable")
        pane.send_key("Enter")
        apply_keystrokes(pane, render_keystrokes(ops), pace_for(ops, self.pace_seconds))
        pane.send_text(":setlocal nomodifiable")
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
