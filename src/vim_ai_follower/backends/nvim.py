"""First-class Neovim follower backend. Unlike the retired nvim_rpc stub, this
drives a dedicated (or adopted) nvim entirely over msgpack-RPC: a Python control
loop walks the edit at line boundaries — checking control.check_signal exactly
like animate.run_lines/run_ops — and dispatches each line to a Lua snippet
(nvim_lua.TYPE_LINE) that types it char-by-char, paces with vim.wait, highlights
it with an extmark, and moves the cursor. No `tmux send-keys`, so the keystroke-
corruption bug class the tmux backend fights simply does not exist here."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pynvim

from vim_ai_follower import control
from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, AnimationResult
from vim_ai_follower.backends import nvim_lua
from vim_ai_follower.diff import EditOp

_NAMESPACE = "vaf"


def _animate_lines(
    nvim: pynvim.Nvim,
    buf: int,
    lines: tuple[str, ...],
    start_row: int,
    pace_provider: Callable[[], float],
    window_id: str,
    ns: int,
    base_dir: Path | None = None,
) -> AnimationResult:
    """Type `lines` into `buf` from `start_row`, one Lua-dispatched line at a
    time, checking for a pause/interrupt signal at each line boundary (never
    mid-char: TYPE_LINE types a whole line atomically inside nvim). Any signal
    stops the run before that line is typed and returns "interrupted" with the
    count already shown. Full pause-wait/resume + speed re-read parity lands in
    Task 5 — here a signal simply ends the animation, mirroring the returned
    outcomes of animate.run_lines."""
    for index, line in enumerate(lines):
        if control.check_signal(window_id, base_dir) is not None:
            return AnimationResult("interrupted", index)
        pace_ms = int(pace_provider() * 1000)
        nvim.api.exec_lua(nvim_lua.TYPE_LINE, [buf, start_row + index, line, pace_ms, ns])
    return AnimationResult("completed", len(lines))


@dataclass(frozen=True)
class NvimFollower:
    """Follower backend that animates edits in a real Neovim over the RPC API.

    Buffer-per-file navigation (goto_file/ensure_showing) and the launched-vs-
    adopted lock/lifecycle distinction land in later phases (Tasks 5/6); this
    task owns the connection, show_fresh, apply_edit, and the per-line driver."""

    socket_path: str
    window_id: str = ""
    pace_seconds: float = DEFAULT_PACE_SECONDS

    def _connect(self) -> pynvim.Nvim:
        return pynvim.attach("socket", path=self.socket_path)

    def _pace_provider(self) -> float:
        return self.pace_seconds

    def is_alive(self) -> bool:
        try:
            self._connect().api.get_current_buf()
            return True
        except OSError:
            return False

    def _drive(
        self,
        nvim: pynvim.Nvim,
        buf: int,
        run: Callable[[], AnimationResult],
    ) -> AnimationResult:
        """Shared envelope for every animation: unlock the buffer, clear stale
        signals, mark the window animating for its duration, run, then relock
        (nomodifiable) on any non-interrupted outcome. An interrupt hands the
        buffer to the user, so it is deliberately left modifiable."""
        nvim.api.buf_set_option(buf, "modifiable", True)
        control.clear_signals(self.window_id)
        control.mark_animating(self.window_id)
        try:
            result = run()
        finally:
            control.clear_animating(self.window_id)
        if result.outcome != "interrupted":
            nvim.api.buf_set_option(buf, "modifiable", False)
        return result

    def show_fresh(self, file_path: str, content: str, in_new_tab: bool = False) -> AnimationResult:
        """Rebuild the buffer named `file_path` from `content` and type it in.

        Deliberately never `:e`/`:edit` here: that would load the file's real
        (already-written) content and flash the finished result before the
        retype. The buffer is wiped and renamed in place, seeded with a single
        blank line, then each content line is animated in. in_new_tab is
        accepted for protocol parity but multi-buffer layout is Task 6."""
        nvim = self._connect()
        ns = nvim.api.create_namespace(_NAMESPACE)
        nvim.command(f"silent! bwipeout! {file_path}")
        nvim.command("enew")
        nvim.command(f"file {file_path}")
        nvim.command("filetype detect")
        nvim.command("setlocal buftype=")
        buf = nvim.current.buffer.handle
        lines = tuple(content.splitlines())

        def run() -> AnimationResult:
            nvim.api.buf_set_lines(buf, 0, -1, True, [""])
            result = _animate_lines(nvim, buf, lines, 0, self._pace_provider, self.window_id, ns)
            if result.outcome == "completed" and lines:
                # Drop the seed blank line the retype pushed to the bottom, so
                # the buffer holds exactly `content` with no trailing blank.
                nvim.api.buf_set_lines(buf, len(lines), len(lines) + 1, True, [])
            return result

        return self._drive(nvim, buf, run)

    def apply_edit(self, file_path: str, ops: list[EditOp]) -> AnimationResult:
        """Apply an edit script to the current buffer: each op deletes its
        old range (instant, via nvim_buf_set_lines) and animates its new lines
        in, checking for a signal at every op and line boundary."""
        nvim = self._connect()
        ns = nvim.api.create_namespace(_NAMESPACE)
        buf = nvim.api.get_current_buf().handle

        def run() -> AnimationResult:
            for index, op in enumerate(ops):
                if control.check_signal(self.window_id) is not None:
                    return AnimationResult("interrupted", index)
                # start_line-1/end_line are the 0-indexed, end-exclusive range
                # nvim_buf_set_lines wants (same convention as diff.apply_ops).
                nvim.api.buf_set_lines(buf, op.start_line - 1, op.end_line, True, [])
                if op.new_lines:
                    result = _animate_lines(
                        nvim,
                        buf,
                        op.new_lines,
                        op.start_line - 1,
                        self._pace_provider,
                        self.window_id,
                        ns,
                    )
                    if result.outcome == "interrupted":
                        return AnimationResult("interrupted", index)
            return AnimationResult("completed", len(ops))

        return self._drive(nvim, buf, run)

    def goto_file(self, file_path: str) -> None:
        # Buffer-per-file navigation lands in Task 6 (Phase 3). Never `:e` the
        # real file here — that would flash disk content before a retype.
        pass

    def ensure_showing(self, file_path: str) -> None:
        # See goto_file — a no-op until Phase 3 grows buffer navigation.
        pass

    def close_tab(self, file_path: str) -> None:
        self._connect().command(f"silent! bwipeout! {file_path}")

    def goto_line(self, offset: int) -> None:
        self._connect().current.window.cursor = (offset, 0)

    def stop(self) -> None:
        # No-op: never kill the user's editor from here. Launched-nvim
        # lifecycle (quit the dedicated instance) is wired in a later phase.
        pass
