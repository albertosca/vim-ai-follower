from __future__ import annotations

import sys

from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane, TmuxSession


def cmd_start(env: dict[str, str]) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux", file=sys.stderr)
        return 1
    if FollowerState.get(session.session_id) is not None:
        print("claude-follow: follower already running for this session")
        return 0
    pane = TmuxPane.split_from(env["TMUX_PANE"], "vim")
    FollowerState.set(session.session_id, pane)
    print(f"claude-follow: started follower in pane {pane.pane_id}")
    return 0


def cmd_stop(env: dict[str, str]) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux", file=sys.stderr)
        return 1
    existing = FollowerState.get(session.session_id)
    if existing is not None:
        existing.pane.kill()
    FollowerState.clear(session.session_id)
    print("claude-follow: stopped")
    return 0


def cmd_status(env: dict[str, str]) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux")
        return 0
    existing = FollowerState.get(session.session_id)
    if existing is None:
        print("claude-follow: no follower active")
    else:
        print(
            f"claude-follow: active, pane {existing.pane.pane_id}, showing {existing.current_file}"
        )
    return 0
