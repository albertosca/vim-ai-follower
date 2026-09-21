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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pynvim

from vim_ai_follower import config, control
from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, AnimationResult, _wait_while_paused
from vim_ai_follower.control import PendingApplyEdit, PendingShowFresh
from vim_ai_follower.diff import EditOp, apply_ops
from vim_ai_follower.state import FollowerState

_NAMESPACE = "vaf"
_TYPING_HL = "VafTypingLine"


def _terminated(lines: Sequence[str]) -> str:
    """The buffer content of `lines` in the terminated-newline form every
    persisted partial uses (hooks._terminated is the same function on the hook
    side; duplicated rather than imported because hooks imports this module).
    Terminating beats joining: a "\\n".join round-trip through splitlines
    drops a trailing blank line, this one is lossless."""
    return "".join(line + "\n" for line in lines)


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

    On interrupt mid-line, the buffer is left exactly as far as it was typed —
    no snap to the line's full text. Alberto's report (2026-09-16): with the
    old snap, interrupting mid-line "finished writing the line" before handing
    the buffer over, which is precisely what he does not want. The line is NOT
    counted as shown (the run returns "interrupted" with the line's own index,
    not index + 1), so it is the first entry of the saved remainder and gets
    retyped from its start on resume — the whole-line partial/resume machinery
    stays coherent (it always retypes a stored remainder line from scratch,
    never mid-character) without any consumer needing to know a line was ever
    half-typed. This mirrors the mid-char PAUSE crash-fallback below
    (`save_pending(index)`), which already treats the in-progress line as not
    yet shown for the same reason. A pause blocks in place via
    animate._wait_while_paused until the user resumes (retyping from the same
    character) or interrupts. The pace is re-read per line so a live Ctrl+a
    +/- takes effect at the next line; a zero pace types the whole line at
    once (the pace-0 catch-up must not animate).
    `save_pending(index)` persists the crash-fallback remainder while paused."""
    # Static default (2026-09-15, Alberto's request): his real nvim config
    # loads gruvbox via ~/.vimrc, but that config's own plugin loading can
    # still be mid-flight when typing starts, so force it explicitly and
    # synchronously here instead of racing load order. Order matters twice
    # over: `set background=dark` must run BEFORE `colorscheme` (gruvbox
    # reads &background at source time; setting it after forces a second,
    # redundant re-source), and BOTH must run before `highlight default
    # {_TYPING_HL}` below — `:colorscheme` always runs `hi clear` first,
    # which wipes every Vaf* highlight group already defined (VafTypingLine
    # here, and VafWriterCue from hooks._apply_writer_cue), silently and
    # invisibly disabling the typing cue and transiently blanking the
    # writer-cue border. The g:colors_name guard makes this a true no-op
    # after the first animation on this nvim process, so only the very
    # first call pays the hi clear cost (both self-heal afterward via
    # hooks._refresh_writer_cue's completion re-render). `silent!` swallows
    # "E185: Cannot find color scheme" in hermetic test/CI nvims that don't
    # have the gruvbox plugin at all; `background` is a built-in option and
    # needs no guard. `silent!` must sit directly before `colorscheme`, not
    # before `if` — a modifier on `if` does NOT propagate to guard the
    # command(s) it guards (verified against real nvim: `silent! if 1 |
    # colorscheme bogus | endif` still raises E185, while `if 1 | silent!
    # colorscheme bogus | endif` swallows it).
    nvim.command("set background=dark")
    nvim.command(
        "if get(g:, 'colors_name', '') !=# 'gruvbox' | silent! colorscheme gruvbox | endif"
    )
    nvim.command(f"highlight default {_TYPING_HL} ctermbg=237 guibg=#3a3a3a")
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
        nvim.api.buf_del_extmark(buf, ns, mark)
        if interrupted:
            # Leave the line exactly as far as it was typed (no snap) and
            # don't count it as shown — see the docstring above.
            return AnimationResult("interrupted", index)
        index += 1
    return AnimationResult("completed", len(lines))


@dataclass(frozen=True)
class NvimFollower:
    """Follower backend that animates edits in a real Neovim over the RPC API.

    Real-tab navigation (goto_file/ensure_showing, via pure API tabpage/
    window lookups — never an Ex command) gives multi-file parity with the
    tmux backend; this class also owns the connection, show_fresh,
    apply_edit, the per-line driver, full pause/resume + live-speed parity,
    and the launched-vs-adopted relock distinction."""

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
        blank line, then each content line is animated in. in_new_tab opens a
        fresh tab first (:tabnew, no disk read) instead of reusing the
        current window — real multi-file parity with the tmux backend."""
        nvim = self._connect()
        ns = nvim.api.create_namespace(_NAMESPACE)
        nvim.command(f"silent! bwipeout! {file_path}")
        if in_new_tab:
            nvim.command("tabnew")
        else:
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
                    # The buffer was wiped to a bare seed blank before this
                    # run, so the fully-typed lines ARE the whole partial. The
                    # line being typed when a pause lands is deliberately
                    # absent: it is the remainder's first entry and gets
                    # retyped from scratch, so counting it here too is exactly
                    # what duplicated it on the later catch-up.
                    partial=_terminated(lines[:index]),
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
        # The buffer is still clean here (nothing typed yet), so this is the
        # one honest reading of "what was on screen before this run" — the
        # base every persisted partial below is computed from. Read once:
        # mid-run the buffer may end on a half-typed line, which is precisely
        # the shape a crash-fallback consumer must not have to interpret.
        initial = _terminated(nvim.api.buf_get_lines(buf, 0, -1, True))
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
                    # Op-granular on this side too: the partly-typed op
                    # contributes nothing, exactly as the remainder replays it
                    # whole. Each op's line numbers are relative to the state
                    # its predecessors produced, so replaying the prefix onto
                    # `initial` reproduces the buffer without reading it.
                    partial=apply_ops(initial, ops[:_op]),
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
            # An op whose range spans the buffer's own top-to-bottom (starts
            # at line 1 and reaches at least the current last line) empties
            # it, and nvim can never hold zero lines — it silently keeps one
            # implicit blank line, the same role show_fresh's own seed blank
            # plays (see its docstring). _animate_lines always inserts each
            # new line BEFORE its target row, so that implicit blank gets
            # pushed past every typed line instead of being consumed by one.
            # Snapshot the pre-delete count now: after the delete call it can
            # no longer tell a genuinely-emptied buffer from one that already
            # held a single real line.
            wipes_buffer = op.start_line == 1 and op.end_line >= nvim.api.buf_line_count(buf)
            # EditOp carries no old text, so keep the range we are about to
            # delete: an interrupt mid-op has to put it back (see below). Only
            # an op that animates new lines can stop mid-op — a delete-only op
            # runs to completion between two signal checks — so only that one
            # pays for the snapshot.
            old_lines = (
                nvim.api.buf_get_lines(buf, op.start_line - 1, op.end_line, True)
                if op.new_lines
                else []
            )
            nvim.api.buf_set_lines(buf, op.start_line - 1, op.end_line, True, [])
            if op.new_lines:
                # How many rows sit where the op's range used to be, right
                # after the delete: zero when the delete emptied the buffer
                # (the one line nvim insists on keeping is the implicit
                # blank, not content).
                rows_after_delete = nvim.api.buf_line_count(buf) - (1 if wipes_buffer else 0)
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
                    # Roll the op back so the buffer the user takes over is
                    # exactly apply_ops(before, ops[:index]) — the state the
                    # interrupt notification quotes to Claude as "what was
                    # shown". Without this the old range is already gone while
                    # `index` says the op never happened, and the notification
                    # over-reports the buffer by every line this op deleted.
                    # The tmux backend gets the same guarantee from its `u`
                    # undos; here `:undo` is unusable — one undo entry per
                    # typed character, and in an adopted nvim the undo tree
                    # belongs to the user — so restore the region by hand.
                    # Its length is measured, not derived: _animate_lines
                    # inserts a blank row per line before typing into it, so a
                    # half-typed line occupies a row too, and a wiping op's
                    # implicit blank has to be swallowed by the same write or
                    # the restored buffer would carry a spurious trailing one.
                    occupied = nvim.api.buf_line_count(buf) - rows_after_delete
                    start = op.start_line - 1
                    nvim.api.buf_set_lines(buf, start, start + occupied, True, old_lines)
                    return AnimationResult("interrupted", index)
                if wipes_buffer:
                    # Drop the implicit blank now pushed past every typed
                    # line, mirroring show_fresh's own seed-blank cleanup.
                    # A delete-only op (no new_lines, handled above) never
                    # reaches here: the implicit blank IS the correct final
                    # state for a buffer emptied with nothing to replace it.
                    drop = op.start_line - 1 + len(op.new_lines)
                    nvim.api.buf_set_lines(buf, drop, drop + 1, True, [])
            index += 1
        return AnimationResult("completed", len(ops))

    def apply_edit(
        self, file_path: str, ops: list[EditOp], before: str | None = None
    ) -> AnimationResult:
        """Apply an edit script to file_path's buffer: each op deletes its
        old range (instant, via nvim_buf_set_lines) and animates its new lines
        in, checking for a signal at every op and line boundary.

        `before` is part of the Follower protocol for the tmux backend, which
        sends keystrokes and can never read the buffer back, so it must be told
        the base its crash-fallback `partial` is computed from. _run_ops reads
        this buffer directly at run start (the one honest reading of what was
        on screen), so the argument is redundant here — ignored.

        The ops were computed against a SNAPSHOT of that buffer, so they are
        meaningless without it. When the buffer is gone (an adopted nvim
        restarted on the same socket, or the user :bwipeout'd it) goto_file
        would hand us a brand-new EMPTY buffer and the op-loop would either
        fabricate wrong content (small ops "succeed") or raise nvim's "Index
        out of bounds" on any op touching a later line — uncaught, that
        escapes the hook process. Show the real on-disk content instead: this
        runs from PostToolUse, after Claude's write, so disk already holds
        exactly the post-edit content. Reported as completed(len(ops)) so the
        caller's bookkeeping (writer-cue refresh, open-file tracking) runs its
        normal non-interrupted path — nothing downstream assumes the
        animation actually typed. This branch never reaches _drive, so
        _open_from_disk locks the buffer itself: a completed animation
        leaves the buffer locked on both backends, and this is the one path
        that returns "completed" without going through the machinery that
        normally guarantees it."""
        del before
        nvim = self._connect()
        if nvim.funcs.bufnr(file_path) == -1:
            self._open_from_disk(nvim, file_path)
            return AnimationResult("completed", len(ops))
        self.goto_file(file_path)
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
        # Everything above start_row is the partial this replay is continuing
        # from — the seed blank, when there is one, sits below it and is not
        # part of the content. _animate_lines only ever inserts at or after
        # start_row, so these rows stay valid for the whole run.
        prefix = existing[:start_row]

        def save_pending(index: int) -> None:
            control.save_pending_show_fresh(
                self.window_id,
                lines[index:],
                self._pace_provider(),
                continuation=start_row + index > 0,
                file_path=file_path,
                partial=_terminated([*prefix, *lines[:index]]),
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
        (parity with tmux.resume).

        When the remainder's buffer is GONE, the replay is abandoned whole and
        NOTHING is touched — no navigation, no buffer creation, no relock.
        Deliberately not a disk load, unlike ensure_showing/apply_edit: loading
        here would leave the buffer holding the POST-edit disk content, and the
        apply_edit that hooks._animate_edit runs right after this (the pace-0
        catch-up precedes the new edit) would then find bufnr != -1 and animate
        before->after ops on top of after-content — garbage, or the same "Index
        out of bounds", one call later. Leaving the buffer absent hands the
        decision to apply_edit's own guard (or to show_fresh on the is_fresh
        path), which is the one with the content to show. Reported as completed
        for the whole remainder so no caller treats it as an interrupt. The
        des-interrupt replay can never reach this branch: hooks._await_user_
        handoff always calls rewrite_buffer first, which goes through goto_file
        and therefore recreates the buffer, and only resumes when that returned
        completed."""
        nvim = self._connect()
        if pending.file_path and nvim.funcs.bufnr(pending.file_path) == -1:
            remaining = pending.ops if isinstance(pending, PendingApplyEdit) else pending.lines
            return AnimationResult("completed", len(remaining))
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
        interrupted. Acts on whatever buffer is current — safe here because
        it always runs immediately after resume() left the right tab
        current; unlike apply_edit, it has no independent file_path to
        navigate to."""
        nvim = self._connect()
        buf = nvim.api.get_current_buf().handle
        nvim.api.buf_set_option(buf, "modifiable", True)

    def goto_file(self, file_path: str) -> None:
        """Switch to the window showing file_path (by name, immune to the
        user closing/reordering tabs), opening one if missing. Checks every
        window of every tab, not just each tab's focused window — a buffer
        can be visible in an unfocused split (normal in an adopted nvim, the
        user's own editor), and checking only the focused window would miss
        it and open a duplicate tab for the same file (measured live,
        2026-09-15). set_current_win switches tabpage AND window in one
        call. Pure API calls only — never an Ex :edit/:drop/:buffer, which
        would trigger Vim's "abandon unsaved changes" guard (E37) or
        silently discard typed-but-unsaved content and reload from disk,
        exactly what this backend must never do (its buffers are never
        written; see show_fresh). This is the entry point that NEVER reads
        disk — a missing buffer is created EMPTY here, because show_fresh's
        callers must not see the finished file flashed before it is typed.
        ensure_showing is the sibling that does read disk. Looked up via
        nvim.funcs.bufnr rather
        than a hand-rolled compare, for the same canonicalization reason as
        before (macOS /tmp -> /private/tmp)."""
        nvim = self._connect()
        bufnr = nvim.funcs.bufnr(file_path)
        if bufnr != -1:
            for tabpage in nvim.api.list_tabpages():
                for win in nvim.api.tabpage_list_wins(tabpage):
                    if nvim.api.win_get_buf(win).number == bufnr:
                        nvim.api.set_current_win(win)
                        return
            # Buffer exists but isn't shown in any window — reachable (e.g.
            # after a show_fresh(in_new_tab=False) hijacks the one existing
            # tab, or after eviction touches a stale reference) — show it in
            # a new tab via API rather than risk any Ex command's
            # unsaved-changes guard.
            nvim.command("tabnew")
            nvim.api.win_set_buf(0, bufnr)
            return
        # No such buffer at all — create one fresh, in a new tab.
        nvim.command("tabnew")
        buf = nvim.api.create_buf(True, False)
        nvim.api.buf_set_name(buf, file_path)
        nvim.api.win_set_buf(0, buf)

    def _open_from_disk(self, nvim: pynvim.Nvim, file_path: str) -> None:
        """Open file_path's REAL on-disk content in a new tab.

        Pure API (bufadd + bufload + win_set_buf) rather than `:edit`, for two
        measured reasons (2026-09-16, real headless nvim). First, `:edit`
        needs the path escaped: a perfectly ordinary name containing `#` or
        `%` raises E499 ("Empty file name for '%' or '#'"), while bufadd takes
        the path as data. Second, bufload only loads a buffer that is NOT
        already loaded, so it can never discard typed-but-unsaved content —
        no "abandon changes" guard (E37) is even in play, whereas `:edit`'s
        safety relies on the argument that tabnew's scratch window makes E37
        unreachable. bufadd applies nvim's own name canonicalization (macOS
        /tmp -> /private/tmp), so a later bufnr(file_path) finds this very
        buffer — same reason goto_file uses bufnr. bufadd creates the buffer
        unlisted; list it, for parity with goto_file's create_buf(True, ...).
        A file that does not exist on disk is fine: the buffer opens empty,
        exactly as `:edit` on a new file would.

        Locked (nomodifiable) before returning when this is a launched,
        dedicated follower — nothing animates this buffer afterwards, so it
        must land in the same locked state ensure_showing's other branch
        does. An adopted nvim is the user's own editor and is never locked
        by this backend, navigation included: the controller's earlier
        ruling to mirror tmux's unconditional lock here (2026-09-16) has
        been reversed, restoring the same launched/adopted split `_drive`'s
        completion relock uses (see its docstring — locking the user out of
        their own buffer is hostile regardless of which entry point does
        it). Shared by both of this backend's disk-reading callers:
        ensure_showing (Read navigation) and apply_edit's vanished-buffer
        branch, where a "completed" outcome must leave the buffer locked
        exactly like a completed animation run through _drive would, unless
        adopted."""
        bufnr = nvim.funcs.bufadd(file_path)
        nvim.funcs.bufload(bufnr)
        nvim.api.buf_set_option(bufnr, "buflisted", True)
        nvim.command("tabnew")
        nvim.api.win_set_buf(0, bufnr)
        nvim.command("filetype detect")
        if not self._is_adopted():
            nvim.api.buf_set_option(bufnr, "modifiable", False)

    def ensure_showing(self, file_path: str) -> None:
        """Show file_path with its REAL on-disk content — the Read-navigation
        and binary-file entry point, where the finished content is exactly
        what should appear because nothing is animated afterwards (parity
        with the tmux backend's `:tab drop` + its `_LOCK_READONLY`).

        This is THE disk-reading entry point of this backend; goto_file is
        the one that NEVER reads disk, because show_fresh's callers rely on
        the finished file not being flashed before it is typed. An existing
        buffer is therefore switched to and never reloaded: it may hold
        typed-but-unsaved content, which in this backend is the norm.

        Both branches lock the buffer (nomodifiable) before returning, via
        the same `buf_set_option` call `_drive`'s completion relock uses —
        a stray keystroke here must not corrupt a buffer that isn't being
        actively animated, exactly the tmux backend's rationale for locking
        after `:tab drop` — but, like `_drive`, ONLY for a launched,
        dedicated follower. An adopted nvim is the user's own editor and is
        never locked by this backend, navigation included: a controller
        ruling on 2026-09-16 made this lock unconditional to mirror tmux
        exactly, which contradicted this backend's own design (and the
        README's promise that an adopted editor is never locked read-only);
        that ruling has since been reversed. See `_drive`'s docstring for
        why locking the user out of their own buffer is hostile regardless
        of which entry point would do it. `hand_over` is still how a
        launched follower's buffer comes back to the user."""
        nvim = self._connect()
        if nvim.funcs.bufnr(file_path) != -1:
            self.goto_file(file_path)
            buf = nvim.api.get_current_buf().handle
            if not self._is_adopted():
                nvim.api.buf_set_option(buf, "modifiable", False)
            return
        self._open_from_disk(nvim, file_path)

    def close_tab(self, file_path: str) -> None:
        # bufnr() here too, for the same reason as goto_file: bwipeout by a
        # raw (unresolved) name is a no-op if nvim canonicalized the buffer's
        # actual name, silently leaving the "evicted" buffer alive. goto_file
        # first for structural parity with reload_and_relock/rewrite_buffer —
        # measured that bwipeout! alone (without navigating there first)
        # already closes the right tab regardless of which one is current,
        # so this preamble isn't load-bearing, just consistent style.
        self.goto_file(file_path)
        nvim = self._connect()
        bufnr = nvim.funcs.bufnr(file_path)
        if bufnr != -1:
            nvim.command(f"silent! bwipeout! {bufnr}")

    def goto_line(self, offset: int) -> None:
        """Move the cursor to line `offset`, CLAMPED to the buffer's last
        line. An out-of-range Read offset is normal — Claude can read a file
        at an offset that a later edit made shorter — and nvim's cursor
        setter answers it with "Invalid cursor line: out of range", which
        escapes the hook process uncaught. The caller only ever passes
        offset > 0, so a lower clamp would be dead code."""
        nvim = self._connect()
        nvim.current.window.cursor = (min(offset, nvim.api.buf_line_count(0)), 0)

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
