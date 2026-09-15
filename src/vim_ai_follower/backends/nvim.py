"""First-class Neovim follower backend. Unlike the retired nvim_rpc stub, this
drives a dedicated (or adopted) nvim entirely over msgpack-RPC: a Python control
loop types each edit one CHARACTER at a time over the API — checking
control.check_signal before every character, exactly like animate.run_lines/
run_ops do per keystroke — highlighting the active line with an extmark, moving
the cursor, and forcing a redraw so the typing shows smoothly. No `tmux
send-keys`, so the keystroke-corruption bug class the tmux backend fights simply
does not exist here."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pynvim

from vim_ai_follower import config, control
from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, AnimationResult, _wait_while_paused
from vim_ai_follower.control import PendingApplyEdit, PendingShowFresh
from vim_ai_follower.diff import EditOp
from vim_ai_follower.state import FollowerState

_NAMESPACE = "vaf"
_TYPING_HL = "VafTypingLine"


def _bind_save(save_pending: Callable[[int], None] | None, index: int) -> Callable[[], None]:
    """A zero-arg callable persisting the crash-fallback remainder from `index`
    (or a no-op when no save_pending was given), as _wait_while_paused wants."""

    def _save() -> None:
        if save_pending is not None:
            save_pending(index)

    return _save


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
    """Type `lines` into `buf` from `start_row`, one CHARACTER at a time over
    the API, checking for a pause/interrupt signal before every character (not
    just between lines) so both respond immediately — the per-keystroke
    granularity the tmux backend gets from run_lines. A redraw per character
    flushes the terminal UI so the typing shows smoothly instead of in
    coalesced bursts.

    On interrupt the current line is snapped to its full text — leaving the
    buffer at a clean line boundary, since the partial/resume machinery is
    whole-line — and the run returns "interrupted" with that line counted. A
    pause blocks in place via animate._wait_while_paused until the user resumes
    (retyping from the same character) or interrupts. The pace is re-read per
    line so a live Ctrl+a +/- takes effect at the next line; a zero pace types
    the whole line at once (the pace-0 catch-up must not animate).
    `save_pending(index)` persists the crash-fallback remainder while paused."""
    nvim.command(f"highlight default {_TYPING_HL} ctermbg=237 guibg=#3a3a3a")
    # Static default (2026-09-15, Alberto's request): his real nvim config
    # loads gruvbox via ~/.vimrc, but that config's own plugin loading can
    # still be mid-flight when typing starts, so force it explicitly and
    # synchronously here instead of racing load order. `silent!` swallows
    # "E185: Cannot find color scheme" in hermetic test/CI nvims that don't
    # have the gruvbox plugin at all; `background` is a built-in option and
    # needs no guard.
    nvim.command("silent! colorscheme gruvbox")
    nvim.command("set background=dark")
    index = 0
    while index < len(lines):
        signal = control.check_signal(window_id, base_dir)
        if signal == "interrupt":
            return AnimationResult("interrupted", index)
        if signal == "pause":
            if not _wait_while_paused(window_id, _bind_save(save_pending, index), base_dir):
                return AnimationResult("interrupted", index)
            continue  # resumed: retype this line from its clean boundary
        row = start_row + index
        nvim.api.buf_set_lines(buf, row, row, True, [""])
        line = lines[index]
        pace = pace_provider()
        if pace <= 0:  # pace-0 catch-up: type the whole line at once, no anim
            if line:
                nvim.api.buf_set_text(buf, row, 0, row, 0, [line])
            index += 1
            continue
        mark = nvim.api.buf_set_extmark(buf, ns, row, 0, {"line_hl_group": _TYPING_HL})
        char = 0
        interrupted = False
        while char < len(line):
            signal = control.check_signal(window_id, base_dir)
            if signal == "interrupt":
                interrupted = True
                break
            if signal == "pause":
                if not _wait_while_paused(window_id, _bind_save(save_pending, index), base_dir):
                    interrupted = True
                    break
                continue  # resumed: retype from the same character
            nvim.api.buf_set_text(buf, row, char, row, char, [line[char]])
            char += 1
            with contextlib.suppress(Exception):
                nvim.api.win_set_cursor(0, [row + 1, char])
            nvim.command("redraw")
            time.sleep(pace)
        if char < len(line):  # snap the untyped remainder on interrupt
            nvim.api.buf_set_text(buf, row, char, row, char, [line[char:]])
        nvim.api.buf_del_extmark(buf, ns, mark)
        if interrupted:
            return AnimationResult("interrupted", index + 1)
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

    def _run_ops(
        self,
        nvim: pynvim.Nvim,
        buf: int,
        ns: int,
        ops: list[EditOp],
        pace_provider: Callable[[], float],
        file_path: str,
    ) -> AnimationResult:
        """The shared op-loop behind both apply_edit and resume: each op
        deletes its old range (instant, via nvim_buf_set_lines) and animates
        its new lines in with `pace_provider`, checking for a signal at every
        op and line boundary. On a pause it persists the op-granular remainder
        (the current op onward) as the crash fallback and blocks until resume
        or interrupt — the same contract as animate.run_ops."""
        index = 0
        while index < len(ops):
            op = ops[index]

            # One callback serving both boundaries: _wait_while_paused calls it
            # with no args (op-boundary pause), _animate_lines with the line
            # index (mid-op pause) — either way the saved remainder is
            # op-granular (the current op onward). The persisted pace is the
            # live pace, so a crash-fallback replay picks up the user's speed;
            # a live nvim resume just continues typing.
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
                    pace_provider,
                    self.window_id,
                    ns,
                    save_pending=save_pending,
                )
                if result.outcome == "interrupted":
                    return AnimationResult("interrupted", index)
            index += 1
        return AnimationResult("completed", len(ops))

    def apply_edit(self, file_path: str, ops: list[EditOp]) -> AnimationResult:
        """Apply an edit script to the current buffer: each op deletes its
        old range (instant, via nvim_buf_set_lines) and animates its new lines
        in, checking for a signal at every op and line boundary."""
        nvim = self._connect()
        ns = nvim.api.create_namespace(_NAMESPACE)
        buf = nvim.api.get_current_buf().handle
        return self._drive(
            nvim,
            buf,
            lambda: self._run_ops(nvim, buf, ns, ops, self._pace_provider, file_path),
        )

    def reload_and_relock(self, file_path: str) -> None:
        """Des-interrupt with NO stored remainder: discard the user's unsaved
        typing by reloading the file Claude wrote (`edit!`), then relock. The
        buffer's name matches that file, so — unlike show_fresh — we WANT the
        reload to load the finished disk content here (the user discarded their
        typing, so the file on disk is the truth). A launched, dedicated
        follower is relocked nomodifiable; an adopted nvim is the user's own
        editor and is never locked out of its own buffer (same rule as
        _drive)."""
        self.goto_file(file_path)
        nvim = self._connect()
        nvim.command("edit!")
        if not self._is_adopted():
            buf = nvim.api.get_current_buf().handle
            nvim.api.buf_set_option(buf, "modifiable", False)

    def rewrite_buffer(self, file_path: str, content: str) -> AnimationResult:
        """Instantly (no animation) rebuild the buffer to `content` — the
        des-interrupt replay needs the buffer back at the interrupt-point state,
        discarding the user's unsaved typing, before resume() replays the
        remainder at live pace. Leaves the buffer UNLOCKED and seedless (exactly
        `content`): resume() runs immediately after and owns the relock."""
        self.goto_file(file_path)
        nvim = self._connect()
        buf = nvim.api.get_current_buf().handle
        nvim.api.buf_set_option(buf, "modifiable", True)
        lines = content.splitlines()
        nvim.api.buf_set_lines(buf, 0, -1, True, lines or [""])
        return AnimationResult("completed", len(lines))

    def _resume_fresh(
        self,
        nvim: pynvim.Nvim,
        buf: int,
        ns: int,
        lines: tuple[str, ...],
        pace_provider: Callable[[], float],
        file_path: str,
        seeded: bool,
    ) -> AnimationResult:
        """Append the remaining whole lines of an interrupted fresh retype to
        the current buffer, landing at EXACTLY the final content.

        The seed subtlety: show_fresh only drops its trailing seed blank on a
        COMPLETED outcome, so the two resume entry points hand us different
        buffer shapes. `seeded` carries that provenance EXPLICITLY — the buffer
        shape can't: a legit trailing blank in the content is indistinguishable
        from the seed by sniffing existing[-1] == "" (that ambiguity was the
        bug). The des-interrupt replay (seeded=False) runs right after
        rewrite_buffer, which rebuilt the buffer to EXACTLY the partial with no
        seed — so append at the very end. The pace-0 consume (seeded=True) runs
        on the LIVE interrupted buffer, which still carries the trailing seed
        blank: type the remainder in front of it and drop it on completion,
        exactly as show_fresh does."""
        existing = nvim.api.buf_get_lines(buf, 0, -1, True)
        start_row = len(existing) - 1 if seeded else len(existing)

        def save_pending(index: int) -> None:
            control.save_pending_show_fresh(
                self.window_id,
                lines[index:],
                self._pace_provider(),
                continuation=start_row + index > 0,
                file_path=file_path,
            )

        result = _animate_lines(
            nvim,
            buf,
            lines,
            start_row,
            pace_provider,
            self.window_id,
            ns,
            save_pending=save_pending,
        )
        if result.outcome == "completed" and seeded:
            # Drop the seed blank the retype pushed to the bottom.
            drop = start_row + len(lines)
            nvim.api.buf_set_lines(buf, drop, drop + 1, True, [])
        return result

    def resume(
        self, pending: PendingApplyEdit | PendingShowFresh, *, seeded: bool = False
    ) -> AnimationResult:
        """Replay a saved animation remainder (des-interrupt live replay, or a
        pace-0 crash-fallback consume). Mirrors the tmux backend: re-select the
        tab, then replay the op-loop (PendingApplyEdit) or append the remaining
        whole lines (PendingShowFresh), wrapped in _drive so a completed replay
        relocks (unless adopted) and an interrupt hands the buffer over.

        `seeded` records whether the current buffer still carries show_fresh's
        trailing seed blank (True for the live pace-0 consume; False for the
        des-interrupt replay onto a seedless rewrite_buffer). Only PendingShow
        Fresh consults it; PendingApplyEdit ignores it.

        The pace-0 catch-up must stay silent forever: pending.pace_seconds == 0
        selects a fixed-0 provider that never re-reads live speed mid-catch-up
        (parity with tmux.resume)."""
        nvim = self._connect()
        ns = nvim.api.create_namespace(_NAMESPACE)
        if pending.file_path:
            self.goto_file(pending.file_path)
        buf = nvim.api.get_current_buf().handle
        provider = (lambda: 0.0) if pending.pace_seconds == 0.0 else self._pace_provider
        if isinstance(pending, PendingApplyEdit):
            return self._drive(
                nvim,
                buf,
                lambda: self._run_ops(nvim, buf, ns, pending.ops, provider, pending.file_path),
            )
        return self._drive(
            nvim,
            buf,
            lambda: self._resume_fresh(
                nvim, buf, ns, pending.lines, provider, pending.file_path, seeded
            ),
        )

    def hand_over(self) -> None:
        """Unlock the current buffer for direct user editing. The interrupt
        path already leaves the buffer modifiable via _drive; this is the
        explicit re-assert used when a des-interrupt replay is itself
        interrupted."""
        nvim = self._connect()
        buf = nvim.api.get_current_buf().handle
        nvim.api.buf_set_option(buf, "modifiable", True)

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
        # Quit the dedicated nvim this follower launched — its tmux split
        # closes with it. cmd_stop only routes a NON-adopted follower here (an
        # adopted nvim goes to close_tab), but guard on _is_adopted anyway:
        # quitting the user's own editor would be hostile. Best-effort — qall!
        # tears down the RPC channel, so the call itself may raise as the
        # socket drops.
        if self._is_adopted():
            return
        with contextlib.suppress(Exception):
            self._connect().command("qall!")
