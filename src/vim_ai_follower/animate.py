from __future__ import annotations

import time
from dataclasses import dataclass

from vim_ai_follower.diff import EditOp
from vim_ai_follower.tmux import TmuxPane

DEFAULT_PACE_SECONDS = 0.05
MAX_ANIMATION_SECONDS = 60.0


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


def apply(
    pane: TmuxPane,
    sequences: list[KeySequence],
    pace_seconds: float,
    max_seconds: float = MAX_ANIMATION_SECONDS,
) -> None:
    """Paces at pace_seconds per sequence, but stops pacing once max_seconds
    of wall-clock time has elapsed — the remaining sequences still get sent,
    just back-to-back with no delay, so a long file degrades to "dumped in a
    block" instead of the hook running long enough to hit its own timeout."""
    deadline = time.monotonic() + max_seconds
    for sequence in sequences:
        if sequence.literal:
            pane.send_text(sequence.text)
        else:
            pane.send_key(sequence.text)
        if pace_seconds > 0 and time.monotonic() < deadline:
            time.sleep(pace_seconds)
