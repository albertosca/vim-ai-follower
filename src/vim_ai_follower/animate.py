from __future__ import annotations

import time
from dataclasses import dataclass

from vim_ai_follower.diff import EditOp
from vim_ai_follower.tmux import TmuxPane

DEFAULT_PACE_SECONDS = 0.05
MAX_ANIMATION_SECONDS = 15.0


@dataclass(frozen=True)
class KeySequence:
    text: str
    literal: bool = True


def render_keystrokes(ops: list[EditOp]) -> list[KeySequence]:
    sequences: list[KeySequence] = []
    for op in ops:
        if op.end_line >= op.start_line:
            sequences.append(KeySequence(f":{op.start_line},{op.end_line}d"))
            sequences.append(KeySequence("Enter", literal=False))
        if op.new_lines:
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


def changed_line_count(ops: list[EditOp]) -> int:
    total = 0
    for op in ops:
        if op.end_line >= op.start_line:
            total += op.end_line - op.start_line + 1
        total += len(op.new_lines)
    return total


def pace_for(ops: list[EditOp], base_pace: float = DEFAULT_PACE_SECONDS) -> float:
    """Caps by estimated total animation time rather than a flat line count,
    so a slow speed on a normal-sized file can't run long enough to blow past
    the Claude Code hook timeout — it just falls back to instant instead."""
    if changed_line_count(ops) * base_pace > MAX_ANIMATION_SECONDS:
        return 0.0
    return base_pace


def apply(pane: TmuxPane, sequences: list[KeySequence], pace_seconds: float) -> None:
    for sequence in sequences:
        if sequence.literal:
            pane.send_text(sequence.text)
        else:
            pane.send_key(sequence.text)
        time.sleep(pace_seconds)
