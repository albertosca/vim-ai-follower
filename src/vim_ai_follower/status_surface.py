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
_WINDOW_HEIGHT = 3
_DEFAULT_TITLE = " claude-follow "
_XTERM_CUBE = (0, 95, 135, 175, 215, 255)


def _cterm_to_hex(number: int) -> str:
    """xterm-256 color index -> "#rrggbb", so the writer cue shows under
    termguicolors (guifg) as well as cterm (ctermfg). Covers the 6x6x6 color
    cube (16-231) and the grayscale ramp (232-255) — the whole range PALETTE
    draws from."""
    if number >= 232:
        level = 8 + 10 * (number - 232)
        return f"#{level:02x}{level:02x}{level:02x}"
    n = number - 16
    r, g, b = _XTERM_CUBE[n // 36], _XTERM_CUBE[(n // 6) % 6], _XTERM_CUBE[n % 6]
    return f"#{r:02x}{g:02x}{b:02x}"


def _centered_box(lines: list[str]) -> list[str]:
    """Pad `lines` (each centered horizontally) with blank rows top and bottom
    so the content sits vertically centered in a _WINDOW_HEIGHT-tall box."""
    top = (_WINDOW_HEIGHT - len(lines)) // 2
    out = [""] * top + [line.center(_WINDOW_WIDTH) for line in lines]
    out += [""] * (_WINDOW_HEIGHT - len(out))
    return out[:_WINDOW_HEIGHT]


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
        if not self._has_saved_state:  # save what was showing FIRST — a second
            # set_state (e.g. handoff cue -> "Writing..." on des-interrupt) must
            # not clobber the original that clear() has to restore.
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
    """Renders cues as a small rounded floating window anchored top-right in a
    running Neovim, plus a subtle virtual-text marker near the cursor. The
    writer identity lives in the window title (the agent name, colored by
    identity) and the tinted border (set_writer); the transient state — Paused,
    the hand-off cue — is centered in the body (set_state). Both survive across
    calls because they live in the nvim process itself (the window's config and
    its fixed-name buffer), not in this short-lived, re-constructed-per-call
    Python object.

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
        # Anchor to the TOP-RIGHT, not the top-left (0,0): a fresh retype types
        # its content and parks the cursor + the virtual-text writer label at
        # the top-left, so a status window there sits directly on top of the
        # animation and hides it (confirmed in the 2026-07-20 live smoke).
        # Placing it flush against the right edge keeps it clear of where the
        # text is being typed.
        columns = int(nvim.api.get_option_value("columns", {}))
        opts = {
            "relative": "editor",
            "width": _WINDOW_WIDTH,
            "height": _WINDOW_HEIGHT,
            "row": 1,
            # -2 leaves room for the rounded border against the right edge.
            "col": max(0, columns - _WINDOW_WIDTH - 2),
            "style": "minimal",
            "border": "rounded",
            "title": _DEFAULT_TITLE,
            "title_pos": "center",
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

    def _mark_cursor(self, nvim: pynvim.Nvim, label: str, color: str | None) -> None:
        """The "at the edit point" cue: a SUBTLE virtual-text extmark near the
        cursor in whatever buffer is currently showing (never the status
        window itself, which is unfocusable). A small dot in the writer's
        color plus the label dimmed to Comment — the box already carries the
        prominent name, so this only whispers "who, right here"."""
        cur_buf = nvim.api.get_current_buf()
        ns = nvim.api.create_namespace(_CURSOR_NAMESPACE)
        nvim.api.buf_clear_namespace(cur_buf, ns, 0, -1)
        row, _col = nvim.api.win_get_cursor(0)
        dot_hl = _HIGHLIGHT_GROUP if color is not None else "Comment"
        nvim.api.buf_set_extmark(
            cur_buf,
            ns,
            row - 1,
            0,
            {"virt_text": [["  ● ", dot_hl], [label, "Comment"]], "virt_text_pos": "eol"},
        )

    def set_writer(self, label: str, color: str | None) -> None:
        """Writer cue: the agent's name in the box title (colored by identity)
        and the border tinted to match — identity only, body blank. Never put
        an activity line here: this cue persists after the animation, so a
        "Writing..." body kept claiming progress forever once the edit was
        done (live finding, 2026-08-25 battery Check 5). Transient text is
        set_state's job. The identity color is set for both cterm and gui
        (guifg), so it shows under termguicolors too."""
        with contextlib.suppress(Exception):
            nvim = self._connect()
            win, buf = self._ensure_window(nvim)
            title_hl = "Title"
            if color is not None:
                number = int(color.removeprefix("colour"))
                nvim.command(
                    f"highlight {_HIGHLIGHT_GROUP} ctermfg={number} guifg={_cterm_to_hex(number)}"
                )
                title_hl = _HIGHLIGHT_GROUP
                nvim.api.win_set_option(win, "winhighlight", f"FloatBorder:{_HIGHLIGHT_GROUP}")
            else:
                nvim.api.win_set_option(win, "winhighlight", "")
            nvim.api.win_set_config(
                win, {"title": [[f" {label} ", title_hl]], "title_pos": "center"}
            )
            # Identity lives in the title; the body stays blank (docstring).
            nvim.api.buf_set_lines(buf, 0, -1, True, _centered_box([]))
            ns = nvim.api.create_namespace(_STATUS_NAMESPACE)
            nvim.api.buf_clear_namespace(buf, ns, 0, -1)
            self._mark_cursor(nvim, label, color)

    def set_state(self, text: str | None) -> None:
        """Transient state (Paused, the hand-off cue): shown centered in the
        box body. The writer title/border set by set_writer persist above it."""
        if text is None:
            self.clear()
            return
        with contextlib.suppress(Exception):
            nvim = self._connect()
            _, buf = self._ensure_window(nvim)
            nvim.api.buf_set_lines(buf, 0, -1, True, _centered_box([text]))

    def clear(self) -> None:
        with contextlib.suppress(Exception):
            nvim = self._connect()
            win = self._find_window(nvim)
            if win is not None:
                buf = nvim.api.win_get_buf(win)
                nvim.api.win_close(win, True)
                # By NUMBER: a pynvim Buffer formats as "<Buffer(handle=N)>",
                # which :bwipeout silently rejects. The scratch buffer is only
                # hidden when its window closes, so the NEXT cue's
                # buf_set_name("vaf-status") then hit E95 and — swallowed by
                # suppress() — the float never came back (measured 2026-09-23).
                nvim.command(f"silent! bwipeout! {buf.number}")
            cur_buf = nvim.api.get_current_buf()
            ns = nvim.api.create_namespace(_CURSOR_NAMESPACE)
            nvim.api.buf_clear_namespace(cur_buf, ns, 0, -1)


def status_surface_for(state: FollowerState, target: str | None = None) -> StatusSurface:
    addr = state.target if target is None else target
    if state.backend == "nvim":
        return NvimStatusSurface(socket_path=addr)
    return TmuxStatusSurface(pane_id=addr)
