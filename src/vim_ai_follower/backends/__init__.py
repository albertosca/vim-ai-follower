from __future__ import annotations

from typing import Protocol

from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, AnimationResult
from vim_ai_follower.control import PendingApplyEdit, PendingShowFresh
from vim_ai_follower.diff import EditOp


class Follower(Protocol):
    """A target that can show and animate file edits. Multi-file navigation
    is tab-based in both backends: the tmux backend's goto_file sends
    `:tab drop` via keystrokes; the nvim backend's goto_file finds or opens
    the tab via its RPC API (never an Ex command, to avoid discarding
    unsaved-but-never-written buffer content — E37 / silent disk reload).
    Either way, close_tab is the generic eviction primitive the hook calls
    on whatever falls past max_tabs."""

    def is_alive(self) -> bool: ...

    def ensure_showing(self, file_path: str) -> None: ...

    def goto_file(self, file_path: str) -> None: ...

    def close_tab(self, file_path: str) -> None: ...

    def apply_edit(self, file_path: str, ops: list[EditOp]) -> AnimationResult: ...

    def show_fresh(
        self, file_path: str, content: str, in_new_tab: bool = False
    ) -> AnimationResult: ...

    def goto_line(self, offset: int) -> None: ...

    def stop(self) -> None: ...

    def reload_and_relock(self, file_path: str) -> None: ...

    def rewrite_buffer(self, file_path: str, content: str) -> AnimationResult: ...

    def resume(
        self, pending: PendingApplyEdit | PendingShowFresh, *, seeded: bool = False
    ) -> AnimationResult: ...

    def hand_over(self) -> None: ...


def get_follower(
    backend: str,
    target: str,
    pace_seconds: float = DEFAULT_PACE_SECONDS,
    window_id: str = "",
) -> Follower:
    if backend == "tmux":
        from vim_ai_follower.backends.tmux_vim import TmuxVimFollower

        return TmuxVimFollower(pane_id=target, pace_seconds=pace_seconds, window_id=window_id)
    if backend == "nvim":
        from vim_ai_follower.backends.nvim import NvimFollower

        return NvimFollower(socket_path=target, window_id=window_id, pace_seconds=pace_seconds)
    raise ValueError(f"unknown backend: {backend!r}")
