"""Resolve whether this run is inside tmux (existing window identity) or
standalone (no tmux) — and, standalone, a stable per-terminal id to key all
follower state on, in place of tmux's #{window_id}.

Also answers the diagnostic follow-up question "if nothing is registered under
the identity I resolved to, who else is out there?" (other_live_followers).
That lives here rather than in state.py because it is about identity, not
about one window's state, and because both the hook path and the CLI need the
same answer — see hooks._warn_lost_window_identity and commands.cmd_status."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from vim_ai_follower import cache
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxWindow


@dataclass(frozen=True)
class Session:
    window_id: str
    origin: str | None
    in_tmux: bool


@dataclass(frozen=True)
class OtherFollower:
    """A live follower registered under some identity other than this run's."""

    window_id: str
    backend: str
    target: str
    current_file: str | None


def other_live_followers(window_id: str) -> list[OtherFollower]:
    """Live followers keyed on an identity other than window_id.

    Purely diagnostic, and deliberately read-only: commands._other_live_follower
    scans the same *.pane files but COLLECTS the dead ones as it goes, which is
    right for a stop and wrong here — a hook must never garbage-collect another
    window's state as a side effect of failing to find its own.

    Liveness is checked through FollowerState.get, so every candidate costs a
    backend probe (a tmux shell-out, an nvim RPC connect). Callers on the hook
    path must throttle this, never run it per edit."""
    others = []
    for path in sorted(cache.CACHE_DIR.glob("*.pane")):
        key = path.stem
        if key == window_id:
            continue
        state = FollowerState.get(key)
        if state is None:
            continue
        others.append(
            OtherFollower(
                window_id=key,
                backend=state.backend,
                target=state.target,
                current_file=state.current_file,
            )
        )
    return others


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
