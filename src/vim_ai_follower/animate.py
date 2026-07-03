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


def _insert_sequences(op: EditOp) -> list[KeySequence]:
    if not op.new_lines:
        return []
    sequences: list[KeySequence] = []
    anchor = op.start_line - 1
    if anchor >= 1:
        sequences.append(KeySequence(f":{anchor}"))
        sequences.append(KeySequence("Enter", literal=False))
        sequences.append(KeySequence("o"))
    else:
        sequences.append(KeySequence("gg"))
        sequences.append(KeySequence("O"))
    last_index = len(op.new_lines) - 1
    for index, line in enumerate(op.new_lines):
        sequences.append(KeySequence(line))
        if index < last_index:
            sequences.append(KeySequence("Enter", literal=False))
    sequences.append(KeySequence("Escape", literal=False))
    return sequences


def _insert_prefix_length(op: EditOp) -> int:
    return 3 if (op.start_line - 1) >= 1 else 2


def render_keystrokes(ops: list[EditOp]) -> list[KeySequence]:
    sequences: list[KeySequence] = []
    for op in ops:
        sequences.extend(_delete_sequences(op))
        sequences.extend(_insert_sequences(op))
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

        insert_seq = _insert_sequences(op)
        if insert_seq:
            result = apply(pane, insert_seq, pace_seconds, session_id, control_base_dir=base_dir)
            if result.outcome != "completed":
                pane.send_key("Escape")
                if result.sent_count >= _insert_prefix_length(op):
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


def render_full_type(lines: tuple[str, ...]) -> list[KeySequence]:
    """Types lines into the current (already-empty) line via `i`, rather than
    `render_keystrokes`'s `gg`/`O`-based positioning — used when the buffer
    was just wiped to a single blank line and the whole file is retyped from
    scratch, so there's no existing line to navigate around."""
    if not lines:
        return []
    sequences: list[KeySequence] = [KeySequence("i")]
    last_index = len(lines) - 1
    for index, line in enumerate(lines):
        sequences.append(KeySequence(line))
        if index < last_index:
            sequences.append(KeySequence("Enter", literal=False))
    sequences.append(KeySequence("Escape", literal=False))
    return sequences


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
