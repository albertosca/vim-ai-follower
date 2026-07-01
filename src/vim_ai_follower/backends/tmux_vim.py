from __future__ import annotations

from dataclasses import dataclass

from vim_ai_follower import diff as diff_module
from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, render_full_type, render_keystrokes
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
        # 'paste' suppresses autoindent/smartindent/cindent for the duration:
        # without it, each Enter in insert mode auto-inserts indentation that
        # then stacks with the leading whitespace already in our own lines.
        pane.send_text(":setlocal modifiable paste")
        pane.send_key("Enter")
        apply_keystrokes(pane, render_keystrokes(ops), self.pace_seconds)
        pane.send_text(":setlocal nomodifiable nopaste")
        pane.send_key("Enter")

    def show_fresh(self, content: str) -> None:
        pane = TmuxPane(pane_id=self.pane_id)
        pane.send_text(":setlocal modifiable paste")
        pane.send_key("Enter")
        # `:e` already loaded the file's real content, so wipe it down to a
        # single blank line first — Vim can't have zero lines — then type
        # everything back in via `i` rather than render_keystrokes's `gg`/`O`,
        # which would leave that leftover blank line stranded at the end.
        pane.send_text(":%d")
        pane.send_key("Enter")
        lines = tuple(content.splitlines())
        apply_keystrokes(pane, render_full_type(lines), self.pace_seconds)
        pane.send_text(":setlocal nomodifiable nopaste")
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
