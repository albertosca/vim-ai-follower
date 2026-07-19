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

from vim_ai_follower import config, control
from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, AnimationResult, _wait_while_paused
from vim_ai_follower.backends import nvim_lua
from vim_ai_follower.diff import EditOp
from vim_ai_follower.state import FollowerState

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
    save_pending: Callable[[int], None] | None = None,
) -> AnimationResult:
    """Type `lines` into `buf` from `start_row`, one Lua-dispatched line at a
    time, checking for a pause/interrupt signal at each line boundary (never
    mid-char: TYPE_LINE types a whole line atomically inside nvim).

    Full pause/resume parity with animate.run_lines: an "interrupt" stops the
    run before the current line is typed and returns "interrupted" with the
    count already shown; a "pause" blocks in place (via animate._wait_while_
    paused) until the user resumes — then the same line is typed — or
    interrupts. A pause always lands at a clean line boundary (TYPE_LINE is
    atomic in nvim), so no partial line needs rolling back. The pace is re-read
    per line from `pace_provider` so a live Ctrl+a +/- takes effect at the next
    boundary. `save_pending(index)` persists the crash-fallback remainder while
    waiting (discarded on resume by _wait_while_paused)."""
    index = 0
    while index < len(lines):
        signal = control.check_signal(window_id, base_dir)
        if signal == "interrupt":
            return AnimationResult("interrupted", index)
        if signal == "pause":

            def _save(index: int = index) -> None:
                if save_pending is not None:
                    save_pending(index)

            if not _wait_while_paused(window_id, _save, base_dir):
                return AnimationResult("interrupted", index)
            continue  # resumed: retype this line from its clean boundary
        pace_ms = int(pace_provider() * 1000)
        nvim.api.exec_lua(nvim_lua.TYPE_LINE, [buf, start_row + index, lines[index], pace_ms, ns])
        index += 1
    return AnimationResult("completed", len(lines))


@dataclass(frozen=True)
class NvimFollower:
    """Follower backend that animates edits in a real Neovim over the RPC API.

    Buffer-per-file navigation (goto_file/ensure_showing) lands in Phase 3;
    this backend owns the connection, show_fresh, apply_edit, the per-line
    driver, full pause/resume + live-speed parity, and the launched-vs-adopted
    relock distinction."""

    socket_path: str
    window_id: str = ""
    pace_seconds: float = DEFAULT_PACE_SECONDS

    def _connect(self) -> pynvim.Nvim:
        return pynvim.attach("socket", path=self.socket_path)

    def _pace_provider(self) -> float:
        """Re-read the live speed from FollowerState each line so a running
        animation reacts to Ctrl+a +/- at its next line boundary (parity with
        the tmux backend's _live_pace). Falls back to the pace this follower
        was constructed with when there's no window state to read."""
        if not self.window_id:
            return self.pace_seconds
        state = FollowerState.read(self.window_id)
        if state is None:
            return self.pace_seconds
        return config.pace_seconds_for(state.speed)

    def _is_adopted(self) -> bool:
        """An adopted nvim is the user's own editor and is never relocked
        after an animation — locking the user out of their own buffer would be
        hostile. A launched, dedicated follower does relock (see _drive). The
        adopted flag is persisted in FollowerState by start/auto-open."""
        state = FollowerState.read(self.window_id)
        return state is not None and state.adopted

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
        (nomodifiable) on a completed outcome — but only for a launched,
        dedicated follower. An adopted nvim is the user's own editor and is
        never relocked. An interrupt hands the buffer to the user, so it is
        deliberately left modifiable regardless."""
        nvim.api.buf_set_option(buf, "modifiable", True)
        control.clear_signals(self.window_id)
        control.mark_animating(self.window_id)
        try:
            result = run()
        finally:
            control.clear_animating(self.window_id)
        if result.outcome != "interrupted" and not self._is_adopted():
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

            def save_pending(index: int) -> None:
                control.save_pending_show_fresh(
                    self.window_id,
                    lines[index:],
                    self._pace_provider(),
                    continuation=index > 0,
                    file_path=file_path,
                )

            result = _animate_lines(
                nvim,
                buf,
                lines,
                0,
                self._pace_provider,
                self.window_id,
                ns,
                save_pending=save_pending,
            )
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
            index = 0
            while index < len(ops):
                op = ops[index]

                # One callback serving both boundaries: _wait_while_paused
                # calls it with no args (op-boundary pause), _animate_lines
                # with the line index (mid-op pause) — either way the saved
                # remainder is op-granular (the current op onward), mirroring
                # animate.run_ops. A nvim resume just continues typing; the
                # pending is only ever consumed by a crashed hook, never
                # replayed live here.
                def save_pending(_line: int = 0, _op: int = index) -> None:
                    control.save_pending_apply_edit(
                        self.window_id,
                        ops[_op:],
                        self._pace_provider(),
                        file_path=file_path,
                    )

                signal = control.check_signal(self.window_id)
                if signal == "interrupt":
                    return AnimationResult("interrupted", index)
                if signal == "pause":
                    if not _wait_while_paused(self.window_id, save_pending, None):
                        return AnimationResult("interrupted", index)
                    continue  # resumed: retry this op from its clean boundary
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
                        save_pending=save_pending,
                    )
                    if result.outcome == "interrupted":
                        return AnimationResult("interrupted", index)
                index += 1
            return AnimationResult("completed", len(ops))

        return self._drive(nvim, buf, run)

    def goto_file(self, file_path: str) -> None:
        """Switch to the buffer named `file_path`, creating it (unnamed,
        listed) if it doesn't exist yet. Never `:e` the real file here — that
        would flash disk content before a retype. Looked up via
        nvim.funcs.bufnr rather than a hand-rolled nvim_list_bufs + string
        compare: nvim CANONICALIZES buffer names (resolves symlinks — e.g.
        macOS /tmp -> /private/tmp), so a raw string compare of file_path
        against nvim_buf_get_name misses an existing buffer whenever a path
        component is a symlink, and the followup buf_set_name then blows up
        with E95 (buffer with that resolved name already exists) instead of
        finding it. bufnr() applies nvim's own normalization, so it agrees
        with whatever name nvim actually gave the buffer."""
        nvim = self._connect()
        bufnr = nvim.funcs.bufnr(file_path)
        if bufnr != -1:
            nvim.api.set_current_buf(bufnr)
        else:
            buf = nvim.api.create_buf(True, False)
            nvim.api.buf_set_name(buf, file_path)
            nvim.api.set_current_buf(buf)

    def ensure_showing(self, file_path: str) -> None:
        self.goto_file(file_path)

    def close_tab(self, file_path: str) -> None:
        # bufnr() here too, for the same reason as goto_file: bwipeout by a
        # raw (unresolved) name is a no-op if nvim canonicalized the buffer's
        # actual name, silently leaving the "evicted" buffer alive.
        nvim = self._connect()
        bufnr = nvim.funcs.bufnr(file_path)
        if bufnr != -1:
            nvim.command(f"silent! bwipeout! {bufnr}")

    def goto_line(self, offset: int) -> None:
        self._connect().current.window.cursor = (offset, 0)

    def stop(self) -> None:
        # No-op: never kill the user's editor from here. Launched-nvim
        # lifecycle (quit the dedicated instance) is wired in a later phase.
        pass
