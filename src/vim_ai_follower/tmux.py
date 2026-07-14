"""Thin subprocess wrappers over the tmux CLI: TmuxPane (send-keys, pane
queries, zoom) and TmuxSession (resolve the session from the environment)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class TmuxPane:
    pane_id: str

    def running_command(self) -> str | None:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_current_command}"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in result.stdout.splitlines():
            pane_id, _, command = line.partition(" ")
            if pane_id == self.pane_id:
                return command
        return None

    def send_text(self, text: str) -> None:
        subprocess.run(
            ["tmux", "send-keys", "-t", self.pane_id, "-l", "--", text],
            check=True,
        )

    def send_key(self, key_name: str) -> None:
        subprocess.run(["tmux", "send-keys", "-t", self.pane_id, key_name], check=True)

    def kill(self) -> None:
        subprocess.run(["tmux", "kill-pane", "-t", self.pane_id], check=False)

    def window_panes(self) -> list[tuple[str, str]]:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-t",
                self.pane_id,
                "-F",
                "#{pane_id} #{pane_current_command}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        panes = []
        for line in result.stdout.splitlines():
            pane_id, _, command = line.partition(" ")
            panes.append((pane_id, command))
        return panes

    def zoomed(self) -> bool:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", self.pane_id, "#{window_zoomed_flag}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() == "1"

    def set_zoomed(self, zoomed: bool) -> None:
        # resize-pane -Z is a blind toggle; assert the desired state instead.
        if self.zoomed() != zoomed:
            subprocess.run(["tmux", "resize-pane", "-Z", "-t", self.pane_id], check=False)

    @classmethod
    def split_from(cls, target_pane: str, command: str) -> TmuxPane:
        result = subprocess.run(
            ["tmux", "split-window", "-h", "-t", target_pane, "-P", "-F", "#{pane_id}", command],
            capture_output=True,
            text=True,
            check=True,
        )
        return cls(pane_id=result.stdout.strip())


def adopt_target(origin: str) -> str | None:
    """Find an existing vim pane in origin's window to adopt as the follower
    target, so a fresh split isn't opened when one is already running."""
    for pane_id, command in TmuxPane(pane_id=origin).window_panes():
        if pane_id != origin and command == "vim":
            return pane_id
    return None


@dataclass(frozen=True)
class TmuxSession:
    session_id: str

    @classmethod
    def from_env(cls, env: dict[str, str]) -> TmuxSession | None:
        pane_id = env.get("TMUX_PANE")
        if not pane_id:
            return None
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{session_id}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        session_id = result.stdout.strip()
        if not session_id:
            return None
        return cls(session_id=session_id)
