"""Per-backend rendering surface for the follower's visual feedback: the
writer color cue, the handoff/paused cue, and their restore-on-exit. Task 7
extracts this behind an interface with zero behavior change for the tmux
backend (the existing hook/command tests are the oracle); Task 8 adds the
nvim floating-window implementation."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane

if TYPE_CHECKING:
    import pynvim

_BORDER_STATUS_OPTION = "pane-border-status"
_STATUS_BUFFER_NAME = "vaf-status"
_STATUS_NAMESPACE = "vaf-status"
_CURSOR_NAMESPACE = "vaf-status-cursor"
_HIGHLIGHT_GROUP = "VafWriterCue"
_WINDOW_WIDTH = 32
_WINDOW_HEIGHT = 2


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


@dataclass
class NvimStatusSurface:
    """Renders cues as a small floating window in a running Neovim (opened
    via nvim_open_win, style="minimal") plus a virtual-text label near the
    cursor in the current buffer. The floating window's buffer holds up to
    two lines: the writer label (set_writer) and the transient hand-off cue
    (set_state) — both survive across calls because they live in the nvim
    process itself, found back by the buffer's fixed name, not in this
    (typically short-lived, re-constructed-per-call) Python object.

    Best-effort like TmuxStatusSurface's tmux calls (which run with
    check=False): any nvim RPC failure — a dead socket, a closed instance —
    is swallowed so it can never fail the hook it's called from. pynvim is
    imported lazily inside each method: this module is imported by hooks.py,
    which must stay importable without the optional `nvim` extra installed."""

    socket_path: str

    def _connect(self) -> pynvim.Nvim:
        import pynvim

        return pynvim.attach("socket", path=self.socket_path)

    def _find_window(self, nvim: pynvim.Nvim) -> Any | None:
        for win in nvim.api.list_wins():
            config = nvim.api.win_get_config(win)
            if config.get("relative", "") == "":
                continue
            buf = nvim.api.win_get_buf(win)
            if nvim.api.buf_get_name(buf).endswith(_STATUS_BUFFER_NAME):
                return win
        return None

    def _open_window(self, nvim: pynvim.Nvim, buf: Any) -> Any:
        opts = {
            "relative": "editor",
            "width": _WINDOW_WIDTH,
            "height": _WINDOW_HEIGHT,
            "row": 0,
            "col": 0,
            "style": "minimal",
            "focusable": False,
            "noautocmd": True,
        }
        return nvim.api.open_win(buf, False, opts)

    def _ensure_window(self, nvim: pynvim.Nvim) -> tuple[Any, Any]:
        """Returns (window, buffer), reusing an existing floating status
        window or creating a fresh scratch buffer + window when there is
        none yet."""
        win = self._find_window(nvim)
        if win is not None:
            return win, nvim.api.win_get_buf(win)
        buf = nvim.api.create_buf(False, True)
        nvim.api.buf_set_name(buf, _STATUS_BUFFER_NAME)
        win = self._open_window(nvim, buf)
        return win, buf

    def _mark_cursor(self, nvim: pynvim.Nvim, label: str) -> None:
        """The "at the edit point" cue: a virtual-text extmark carrying the
        writer label at end-of-line, near the cursor in whatever buffer is
        currently showing (never the status window itself, which is
        unfocusable)."""
        cur_buf = nvim.api.get_current_buf()
        ns = nvim.api.create_namespace(_CURSOR_NAMESPACE)
        nvim.api.buf_clear_namespace(cur_buf, ns, 0, -1)
        row, _col = nvim.api.win_get_cursor(0)
        nvim.api.buf_set_extmark(
            cur_buf,
            ns,
            row - 1,
            0,
            {"virt_text": [[label, _HIGHLIGHT_GROUP]], "virt_text_pos": "eol"},
        )

    def set_writer(self, label: str, color: str | None) -> None:
        with contextlib.suppress(Exception):
            nvim = self._connect()
            _, buf = self._ensure_window(nvim)
            lines = nvim.api.buf_get_lines(buf, 0, -1, True)
            state_line = lines[1] if len(lines) > 1 else None
            new_lines = [label, state_line] if state_line else [label]
            nvim.api.buf_set_lines(buf, 0, -1, True, new_lines)
            ns = nvim.api.create_namespace(_STATUS_NAMESPACE)
            nvim.api.buf_clear_namespace(buf, ns, 0, -1)
            if color is not None:
                number = color.removeprefix("colour")
                nvim.command(f"highlight default {_HIGHLIGHT_GROUP} ctermfg={number}")
                nvim.api.buf_add_highlight(buf, ns, _HIGHLIGHT_GROUP, 0, 0, -1)
            self._mark_cursor(nvim, label)

    def set_state(self, text: str | None) -> None:
        if text is None:
            self.clear()
            return
        with contextlib.suppress(Exception):
            nvim = self._connect()
            _, buf = self._ensure_window(nvim)
            lines = nvim.api.buf_get_lines(buf, 0, -1, True)
            label_line = lines[0] if lines else ""
            nvim.api.buf_set_lines(buf, 0, -1, True, [label_line, text])

    def clear(self) -> None:
        with contextlib.suppress(Exception):
            nvim = self._connect()
            win = self._find_window(nvim)
            if win is not None:
                buf = nvim.api.win_get_buf(win)
                nvim.api.win_close(win, True)
                nvim.command(f"silent! bwipeout! {buf}")
            cur_buf = nvim.api.get_current_buf()
            ns = nvim.api.create_namespace(_CURSOR_NAMESPACE)
            nvim.api.buf_clear_namespace(cur_buf, ns, 0, -1)


def status_surface_for(state: FollowerState, target: str | None = None) -> StatusSurface:
    addr = state.target if target is None else target
    if state.backend == "nvim":
        return NvimStatusSurface(socket_path=addr)
    return TmuxStatusSurface(pane_id=addr)
