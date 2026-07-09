from __future__ import annotations

import time
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


@dataclass(frozen=True)
class ApplyResult:
    outcome: Literal["completed", "paused", "interrupted"]
    sent_count: int


def _delete_sequences(op: EditOp) -> list[KeySequence]:
    if op.end_line < op.start_line:
        return []
    return [KeySequence(f":{op.start_line},{op.end_line}d"), KeySequence("Enter", literal=False)]


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
    sequences.append(KeySequence("Escape", literal=False))
    return sequences, len(prefix)


def render_keystrokes(ops: list[EditOp]) -> list[KeySequence]:
    sequences: list[KeySequence] = []
    for op in ops:
        sequences.extend(_delete_sequences(op))
        insert_seq, _ = _insert_sequences(op)
        sequences.extend(insert_seq)
    return sequences


@dataclass(frozen=True)
class AnimationResult:
    outcome: Literal["completed", "paused", "interrupted"]
    completed_count: int


def run_ops(
    pane: TmuxPane,
    session_id: str,
    ops: list[EditOp],
    pace_seconds: float,
    base_dir: Path | None = None,
) -> AnimationResult:
    control.clear_signals(session_id, base_dir)
    for index, op in enumerate(ops):
        delete_seq = _delete_sequences(op)
        if delete_seq:
            result = apply(pane, delete_seq, pace_seconds, session_id, control_base_dir=base_dir)
            if result.outcome != "completed":
                pane.send_key("Escape")
                return _stop(result.outcome, session_id, ops, index, pace_seconds, base_dir)

        insert_seq, prefix_len = _insert_sequences(op)
        if insert_seq:
            result = apply(pane, insert_seq, pace_seconds, session_id, control_base_dir=base_dir)
            if result.outcome != "completed":
                pane.send_key("Escape")
                if result.sent_count >= prefix_len:
                    pane.send_text("u")
                if delete_seq:
                    # The delete half already ran as its own undo unit; roll
                    # it back too so the buffer sits on a clean op boundary —
                    # otherwise resuming would re-run the delete against
                    # lines that have shifted, and the interrupt notification
                    # would claim less was shown than actually happened.
                    pane.send_text("u")
                return _stop(result.outcome, session_id, ops, index, pace_seconds, base_dir)

    return AnimationResult("completed", len(ops))


def _stop(
    outcome: Literal["paused", "interrupted"],
    session_id: str,
    ops: list[EditOp],
    index: int,
    pace_seconds: float,
    base_dir: Path | None,
) -> AnimationResult:
    if outcome == "paused":
        control.save_pending_apply_edit(session_id, ops[index:], pace_seconds, base_dir)
    return AnimationResult(outcome, index)


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
        KeySequence("Escape", literal=False),
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
    session_id: str,
    lines: tuple[str, ...],
    pace_seconds: float,
    base_dir: Path | None = None,
    continuation: bool = False,
) -> AnimationResult:
    control.clear_signals(session_id, base_dir)
    for index, line in enumerate(lines):
        # The first line types into the wiped buffer's single blank line via
        # `i`; every later line — and every line of a resumed run, whose
        # buffer already holds earlier lines — opens its own line below the
        # cursor via `o`.
        opener = "o" if continuation or index > 0 else "i"
        sequences, undo_threshold = _line_sequences(line, opener)
        result = apply(pane, sequences, pace_seconds, session_id, control_base_dir=base_dir)
        if result.outcome != "completed":
            pane.send_key("Escape")
            if result.sent_count >= undo_threshold:
                pane.send_text("u")
            if result.outcome == "interrupted":
                return AnimationResult("interrupted", index)
            control.save_pending_show_fresh(
                session_id,
                lines[index:],
                pace_seconds,
                continuation=continuation or index > 0,
                base_dir=base_dir,
            )
            return AnimationResult("paused", index)
    return AnimationResult("completed", len(lines))


def apply(
    pane: TmuxPane,
    sequences: list[KeySequence],
    pace_seconds: float,
    session_id: str,
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
        signal = control.check_signal(session_id, control_base_dir)
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
