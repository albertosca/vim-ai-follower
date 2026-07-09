from __future__ import annotations

from typing import Protocol

from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, AnimationResult
from vim_ai_follower.diff import EditOp


class Follower(Protocol):
    """A target that can show and animate file edits, regardless of how."""

    def is_alive(self) -> bool: ...

    def ensure_showing(self, file_path: str) -> None: ...

    def apply_edit(self, ops: list[EditOp]) -> AnimationResult: ...

    def show_fresh(self, file_path: str, content: str) -> AnimationResult: ...

    def goto_line(self, offset: int) -> None: ...

    def stop(self) -> None: ...


def get_follower(
    backend: str,
    target: str,
    pace_seconds: float = DEFAULT_PACE_SECONDS,
    session_id: str = "",
) -> Follower:
    if backend == "tmux":
        from vim_ai_follower.backends.tmux_vim import TmuxVimFollower

        return TmuxVimFollower(pane_id=target, pace_seconds=pace_seconds, session_id=session_id)
    if backend == "nvim_rpc":
        from vim_ai_follower.backends.nvim_rpc import NvimRpcFollower

        return NvimRpcFollower(socket_path=target)
    raise ValueError(f"unknown backend: {backend!r}")
