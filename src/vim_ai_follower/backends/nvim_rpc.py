from __future__ import annotations

import time
from dataclasses import dataclass

import pynvim

from vim_ai_follower import diff as diff_module
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.diff import EditOp

PACE_SECONDS = 0.05


@dataclass(frozen=True)
class NvimRpcFollower:
    """Follower backend that talks to a real, already-running Neovim
    instance over msgpack-RPC — the same buffer the user is looking at,
    not a separate dedicated pane."""

    socket_path: str

    def _connect(self) -> pynvim.Nvim:
        return pynvim.attach("socket", path=self.socket_path)

    def is_alive(self) -> bool:
        try:
            self._connect().api.get_current_buf()
            return True
        except OSError:
            return False

    def ensure_showing(self, file_path: str) -> None:
        nvim = self._connect()
        nvim.command(f"edit {file_path}")

    def goto_file(self, file_path: str) -> None:
        # No tabs in this backend — just navigate to the buffer.
        self.ensure_showing(file_path)

    def apply_edit(self, file_path: str, ops: list[EditOp]) -> AnimationResult:
        # No tab navigation here — this backend has no tabs (see goto_file).
        nvim = self._connect()
        buf = nvim.current.buffer
        for op in ops:
            # start_line/end_line are already 0-indexed, end-exclusive once
            # shifted by -1/-0 respectively — same convention nvim_buf_set_lines
            # uses, for all three op kinds (replace/delete/insert).
            buf[op.start_line - 1 : op.end_line] = list(op.new_lines)
            time.sleep(PACE_SECONDS)
        return AnimationResult("completed", len(ops))

    def show_fresh(self, file_path: str, content: str, in_new_tab: bool = False) -> AnimationResult:
        # No `:edit` here on purpose: it would load the real (already
        # written) file content and flash it before the wipe+retype. Rename
        # the current buffer in place instead of ever loading the real one.
        # in_new_tab is ignored in this backend (no tab support).
        nvim = self._connect()
        nvim.command(f"silent! bwipeout! {file_path}")
        nvim.command(f"file {file_path}")
        nvim.command("filetype detect")
        nvim.current.buffer[:] = []
        return self.apply_edit(file_path, diff_module.compute_edit_script("", content))

    def goto_line(self, offset: int) -> None:
        nvim = self._connect()
        nvim.current.window.cursor = (offset, 0)

    def stop(self) -> None:
        pass
