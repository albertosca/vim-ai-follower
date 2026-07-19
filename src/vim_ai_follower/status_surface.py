"""Per-backend rendering surface for the follower's visual feedback: the
writer color cue, the handoff/paused cue, and their restore-on-exit. Task 7
extracts this behind an interface with zero behavior change for the tmux
backend (the existing hook/command tests are the oracle); an nvim
floating-window implementation is Task 8."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane

_BORDER_STATUS_OPTION = "pane-border-status"


class StatusSurface(Protocol):
    def set_writer(self, label: str, color: str | None) -> None:
        """Persistent border cue: tints the follower's border with the
        writer's color (or clears the tint when color is None) and shows
        the writer's label as the title."""
        ...

    def set_state(self, text: str | None) -> None:
        """Transient cue (e.g. the handoff "Claude waiting" message): shows
        text as the title with the border visible, saving whatever was
        showing first so a matching clear() can restore it. text=None is
        equivalent to clear()."""
        ...

    def clear(self) -> None:
        """Undo the surface's own cue. Restores what set_state saved, if
        anything; otherwise resets to the neutral (no cue) state."""
        ...


@dataclass
class TmuxStatusSurface:
    """Renders cues on a tmux pane's border. Reproduces exactly what
    hooks._apply_writer_cue, commands.cmd_stop's border restore, and
    hooks._await_user_handoff's save/restore did before this extraction."""

    pane_id: str
    _saved_title: str | None = field(default=None, init=False, repr=False)
    _saved_border_status: str | None = field(default=None, init=False, repr=False)
    _has_saved_state: bool = field(default=False, init=False, repr=False)

    def set_writer(self, label: str, color: str | None) -> None:
        pane = TmuxPane(pane_id=self.pane_id)
        pane.set_border_color(color)
        pane.set_window_option(_BORDER_STATUS_OPTION, "top")
        pane.set_title(label)

    def set_state(self, text: str | None) -> None:
        if text is None:
            self.clear()
            return
        pane = TmuxPane(pane_id=self.pane_id)
        self._saved_title = pane.title()
        self._saved_border_status = pane.window_option(_BORDER_STATUS_OPTION)
        self._has_saved_state = True
        pane.set_title(text)
        pane.set_window_option(_BORDER_STATUS_OPTION, "top")

    def clear(self) -> None:
        pane = TmuxPane(pane_id=self.pane_id)
        if self._has_saved_state:
            assert self._saved_title is not None  # set alongside the flag in set_state
            pane.set_title(self._saved_title)
            pane.set_window_option(_BORDER_STATUS_OPTION, self._saved_border_status)
            self._has_saved_state = False
            return
        pane.set_border_color(None)
        pane.set_window_option(_BORDER_STATUS_OPTION, None)


def status_surface_for(state: FollowerState) -> StatusSurface:
    # Task 8: dispatch to an nvim floating-window surface when
    # state.backend == "nvim".
    return TmuxStatusSurface(pane_id=state.target)
