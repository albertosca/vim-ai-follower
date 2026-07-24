"""Resolve whether this run is inside tmux (existing window identity) or
standalone (no tmux) — and, standalone, a stable per-terminal id to key all
follower state on, in place of tmux's #{window_id}."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from vim_ai_follower.tmux import TmuxWindow


@dataclass(frozen=True)
class Session:
    window_id: str
    origin: str | None
    in_tmux: bool


def _standalone_id(env: dict[str, str]) -> str:
    term = env.get("TERM_SESSION_ID")
    if term:
        return f"term-{term}"
    iterm = env.get("ITERM_SESSION_ID")
    if iterm:
        return f"iterm-{iterm}"
    try:
        return "tty-" + Path(os.ttyname(0)).name
    except OSError:
        return "standalone-default"


def resolve_session(env: dict[str, str]) -> Session | None:
    if env.get("TMUX_PANE"):
        window = TmuxWindow.from_env(env)
        if window is None:
            return None
        return Session(window_id=window.window_id, origin=env["TMUX_PANE"], in_tmux=True)
    return Session(window_id=_standalone_id(env), origin=None, in_tmux=False)
