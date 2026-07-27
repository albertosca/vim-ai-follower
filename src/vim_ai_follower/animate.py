"""Keystroke translation and paced send-keys animation of edit ops and
fresh-file retypes, checking for pause/interrupt at each line boundary."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vim_ai_follower import control
from vim_ai_follower.diff import EditOp
from vim_ai_follower.tmux import TmuxPane

DEFAULT_PACE_SECONDS = 0.05
MAX_ANIMATION_SECONDS = 60.0


@dataclass(frozen=True)
class KeySequence:
    text: str
    literal: bool = True


# Two Escapes to leave insert mode, never one. A completion popup (CoC/Copilot)
# maps the first <Esc> to close the popup and STAY in insert mode, so a single
# Escape can leave the follower in insert mode; the next op's ex-command (:Nd)
# or opener (o/gg/O) is then typed as LITERAL text and garbles the buffer —
# the delete does not happen and the command text is written into the file
# (empirically confirmed under a real popup; scripts/repro-mode-escape.sh).
# The second Escape lands whatever consumed the first, the same guarantee
# TmuxVimFollower._normal_mode relies on.
#
# NEVER append <C-\><C-n> here. It was added once as extra insurance against a
# remapped insert-mode <Esc> and it CORRUPTED THE BUFFER ON EVERY ANIMATION, in
# two independent ways (scripts/repro-exit-insert-matrix.sh measures both, with
# Vim reporting its own mode via a ModeChanged autocmd):
#   1. send_paced sleeps pace_seconds between every KeySequence, so the pair is
#      split by the pace. <C-\> and <C-N> are the two halves of ONE atomic Vim
#      command: with a gap, Vim expires the pending <C-\> and runs <C-N> as
#      plain insert-mode keyword completion, replacing the word under the cursor
#      with the first keyword in the buffer.
#   2. Sent after the Escapes, it arrives in NORMAL mode, where <C-\> may be a
#      real user mapping — vim-tmux-navigator binds it to :TmuxNavigatePrevious
#      in the config this was found on — and the trailing <C-N> then moves the
#      cursor down a line, drifting the insert point. (In INSERT mode <C-\> is
#      unmapped, which is why the failure looks order-dependent but is not
#      fixable by reordering: every variant containing the pair corrupts.)
# Two Escapes alone measure clean: against the real config that motivated the
# insurance — which DOES remap insert <Esc>, to vim_ai_autocomplete#EscHandler —
# a full animation leaves Insert once per line, every line, and the buffer comes
# out byte-identical to the source (repro-exit-insert-matrix.sh asserts exactly
# this). WHY the remapped <Esc> does not eat them is NOT established: 'paste' is
# documented to disable insert-mode mappings, and EscHandler may simply pass
# <Esc> through when no suggestion is pending, but probes of the synthetic worst
# case (`inoremap <Esc> <Nop>`) disagreed across runs and several of those
# disagreements traced to harness artifacts rather than to Vim. So treat the
# mechanism as open and the measurement as the ground: if a remapped-<Esc>
# garble ever reappears, re-open that question — do NOT reach for <C-\><C-n>.
_EXIT_INSERT: tuple[KeySequence, ...] = (
    KeySequence("Escape", literal=False),
    KeySequence("Escape", literal=False),
)


@dataclass(frozen=True)
class ApplyResult:
    outcome: Literal["completed", "paused", "interrupted"]
    sent_count: int


def _delete_sequences(op: EditOp) -> list[KeySequence]:
    """One (":Nd", Enter) pair per deleted line — always at start_line,
    since the remaining lines shift up after each delete. Per-line pairs
    make deletions paced and visible like insertions (a single :s,ed made
    whole blocks vanish in one frame), and each pair is its own undo unit
    for the rollback bookkeeping in run_ops."""
    if op.end_line < op.start_line:
        return []
    sequences: list[KeySequence] = []
    for _ in range(op.end_line - op.start_line + 1):
        sequences.append(KeySequence(f":{op.start_line}d"))
        sequences.append(KeySequence("Enter", literal=False))
    return sequences


def _insert_sequences(op: EditOp) -> tuple[list[KeySequence], int]:
    """Keystrokes for an op's insert half, plus the length of its cursor-
    positioning prefix (the sequences before any text lands — once the last
    prefix key is sent, Vim has opened a line, i.e. recorded a change). The
    length is derived from the prefix list itself so the two can't desync."""
    if not op.new_lines:
        return [], 0
    prefix: list[KeySequence] = []
    anchor = op.start_line - 1
    if anchor >= 1:
        prefix.append(KeySequence(f":{anchor}"))
        prefix.append(KeySequence("Enter", literal=False))
        prefix.append(KeySequence("o"))
    else:
        prefix.append(KeySequence("gg"))
        prefix.append(KeySequence("O"))
    sequences = list(prefix)
    last_index = len(op.new_lines) - 1
    for index, line in enumerate(op.new_lines):
        sequences.append(KeySequence(line))
        if index < last_index:
            sequences.append(KeySequence("Enter", literal=False))
    sequences.extend(_EXIT_INSERT)
    return sequences, len(prefix)


@dataclass(frozen=True)
class AnimationResult:
    outcome: Literal["completed", "interrupted"]
    completed_count: int


PAUSE_POLL_SECONDS = 0.1


def _exit_insert_mode(pane: TmuxPane) -> None:
    """Two Escapes before the rollback undo. A completion popup (CoC/Copilot)
    maps the first <Esc> to close the popup and STAY in insert mode, so a
    single Escape followed by `u` would type the `u` literally into the
    buffer (empirically "banu"; see scripts/repro-stray-u.sh). The second
    Escape lands as a real Escape whatever consumed the first — the same
    guarantee TmuxVimFollower._normal_mode relies on (kept separate here to
    avoid coupling animate to the follower; a shared helper is a follow-up)."""
    pane.send_key("Escape")
    pane.send_key("Escape")


def _wait_while_paused(
    window_id: str,
    save_pending: Callable[[], None],
    base_dir: Path | None,
) -> bool:
    """Block in place until the user resumes (True) or interrupts (False).
    Pausing no longer exits the hook — Claude's turn stays held until the
    animation truly finishes. The pending file is still written for the
    duration purely as a crash fallback: if this process dies (e.g. hook
    timeout), the keyboard can still finish the animation visually."""
    save_pending()
    control.mark_animating(window_id, base_dir, state="paused")
    try:
        while True:
            signal = control.check_signal(window_id, base_dir)
            if signal == "interrupt":
                return False
            if signal == "pause":  # the P toggle: a second press resumes
                return True
            time.sleep(PAUSE_POLL_SECONDS)
    finally:
        control.discard_pending_animation(window_id, base_dir)
        control.mark_animating(window_id, base_dir, state="running")


def run_ops(
    pane: TmuxPane,
    window_id: str,
    ops: list[EditOp],
    pace_seconds: float | Callable[[], float],
    base_dir: Path | None = None,
    file_path: str = "",
    on_resume: Callable[[], None] | None = None,
) -> AnimationResult:
    provider: Callable[[], float] = (
        pace_seconds if callable(pace_seconds) else (lambda: pace_seconds)
    )
    control.clear_signals(window_id, base_dir)
    control.mark_animating(window_id, base_dir)
    try:
        index = 0
        while index < len(ops):
            op = ops[index]
            # One evaluation per op — the line-boundary cadence — shared by
            # its delete and insert halves, so the two halves can't end up
            # pacing at different speeds.
            current_pace = provider()

            def save_pending(index: int = index, current_pace: float = current_pace) -> None:
                control.save_pending_apply_edit(
                    window_id, ops[index:], current_pace, base_dir, file_path=file_path
                )

            delete_seq = _delete_sequences(op)
            if delete_seq:
                result = send_paced(
                    pane, delete_seq, current_pace, window_id, control_base_dir=base_dir
                )
                if result.outcome != "completed":
                    _exit_insert_mode(pane)
                    # Each committed (":Nd", Enter) pair is one undo unit;
                    # a half-typed pair was cancelled by the Escape above.
                    for _ in range(result.sent_count // 2):
                        pane.send_text("u")
                    if result.outcome == "interrupted":
                        return AnimationResult("interrupted", index)
                    if not _wait_while_paused(window_id, save_pending, base_dir):
                        return AnimationResult("interrupted", index)
                    if on_resume is not None:
                        on_resume()
                    continue  # resumed: retry this op from its clean boundary

            insert_seq, prefix_len = _insert_sequences(op)
            if insert_seq:
                result = send_paced(
                    pane, insert_seq, current_pace, window_id, control_base_dir=base_dir
                )
                if result.outcome != "completed":
                    _exit_insert_mode(pane)
                    if result.sent_count >= prefix_len:
                        pane.send_text("u")
                    # The delete half already ran, one undo unit per line;
                    # roll all of it back so the buffer sits on a clean op
                    # boundary — otherwise retrying would re-run the delete
                    # against lines that have shifted, and the interrupt
                    # notification would claim less was shown than actually
                    # happened.
                    for _ in range(len(delete_seq) // 2):
                        pane.send_text("u")
                    if result.outcome == "interrupted":
                        return AnimationResult("interrupted", index)
                    if not _wait_while_paused(window_id, save_pending, base_dir):
                        return AnimationResult("interrupted", index)
                    if on_resume is not None:
                        on_resume()
                    continue

            index += 1
        return AnimationResult("completed", len(ops))
    finally:
        # interrupted/completed alike: nothing is animating anymore (a
        # crash-orphaned remainder is owned by the pending file instead)
        control.clear_animating(window_id, base_dir)


def _line_sequences(line: str, opener: str) -> tuple[list[KeySequence], int]:
    """Keystrokes for typing one line during a full retype, plus the undo
    threshold: on an early stop, send `u` only when sent_count >= threshold,
    i.e. only when Vim has actually recorded a change. `o` alone already
    opens a line (a change); `i` alone — or `i` plus an empty text send —
    never creates one, and undoing then would eat the previous line's
    change instead."""
    sequences = [
        KeySequence(opener),
        KeySequence(line),
        *_EXIT_INSERT,
    ]
    if opener == "o":
        threshold = 1
    elif line:
        threshold = 2
    else:
        threshold = len(sequences) + 1
    return sequences, threshold


def run_lines(
    pane: TmuxPane,
    window_id: str,
    lines: tuple[str, ...],
    pace_seconds: float | Callable[[], float],
    base_dir: Path | None = None,
    continuation: bool = False,
    file_path: str = "",
    on_resume: Callable[[], None] | None = None,
) -> AnimationResult:
    provider: Callable[[], float] = (
        pace_seconds if callable(pace_seconds) else (lambda: pace_seconds)
    )
    control.clear_signals(window_id, base_dir)
    control.mark_animating(window_id, base_dir)
    try:
        index = 0
        while index < len(lines):
            line = lines[index]
            # One evaluation per line — the line-boundary cadence.
            current_pace = provider()
            # The first line types into the wiped buffer's single blank line
            # via `i`; every later line — and every line of a resumed run,
            # whose buffer already holds earlier lines — opens its own line
            # below the cursor via `o`.
            opener = "o" if continuation or index > 0 else "i"
            sequences, undo_threshold = _line_sequences(line, opener)
            result = send_paced(pane, sequences, current_pace, window_id, control_base_dir=base_dir)
            if result.outcome != "completed":
                _exit_insert_mode(pane)
                if result.sent_count >= undo_threshold:
                    pane.send_text("u")
                if result.outcome == "interrupted":
                    return AnimationResult("interrupted", index)

                def save_pending(index: int = index, current_pace: float = current_pace) -> None:
                    control.save_pending_show_fresh(
                        window_id,
                        lines[index:],
                        current_pace,
                        continuation=continuation or index > 0,
                        base_dir=base_dir,
                        file_path=file_path,
                    )

                if not _wait_while_paused(window_id, save_pending, base_dir):
                    return AnimationResult("interrupted", index)
                if on_resume is not None:
                    on_resume()
                continue  # resumed: retry this line (same opener, clean boundary)
            index += 1
        return AnimationResult("completed", len(lines))
    finally:
        control.clear_animating(window_id, base_dir)


def send_paced(
    pane: TmuxPane,
    sequences: list[KeySequence],
    pace_seconds: float,
    window_id: str,
    *,
    max_seconds: float = MAX_ANIMATION_SECONDS,
    control_base_dir: Path | None = None,
) -> ApplyResult:
    """Paces at pace_seconds per sequence, checking for a pause/interrupt
    signal before each one (see control.check_signal) and stopping
    immediately — without sending that sequence — if one is found. Also
    stops pacing (but keeps sending) once max_seconds of wall-clock time has
    elapsed, so a long file degrades to "dumped in a block" instead of the
    hook running long enough to hit its own timeout."""
    deadline = time.monotonic() + max_seconds
    for sent_count, sequence in enumerate(sequences):
        signal = control.check_signal(window_id, control_base_dir)
        if signal is not None:
            outcome: Literal["paused", "interrupted"] = (
                "interrupted" if signal == "interrupt" else "paused"
            )
            return ApplyResult(outcome, sent_count)
        if sequence.literal:
            pane.send_text(sequence.text)
        else:
            pane.send_key(sequence.text)
        if pace_seconds > 0 and time.monotonic() < deadline:
            time.sleep(pace_seconds)
    return ApplyResult("completed", len(sequences))
