"""Pure, I/O-free line diffing: compute a bottom-to-top edit script between
two file versions, replay it, and detect binary content."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Literal

EditKind = Literal["replace", "delete", "insert"]


@dataclass(frozen=True)
class EditOp:
    kind: EditKind
    start_line: int
    end_line: int
    new_lines: tuple[str, ...]


def compute_edit_script(before: str, after: str) -> list[EditOp]:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)

    ops: list[EditOp] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            kind: EditKind = "replace"
        elif tag == "delete":
            kind = "delete"
        else:
            kind = "insert"
        ops.append(
            EditOp(
                kind=kind,
                start_line=i1 + 1,
                end_line=i2,
                new_lines=tuple(after_lines[j1:j2]),
            )
        )
    ops.reverse()
    return ops


def apply_ops(before: str, ops: list[EditOp]) -> str:
    """Replay `ops` onto `before` and return the resulting buffer content.

    The return terminates every line with "\\n" instead of joining with it,
    which is the canonical form of a persisted `partial` (hooks._terminated
    builds the show_fresh side the same way). Every caller here reconstructs a
    partial that is later `.splitlines()`'d again — by rewrite_buffer, by the
    next apply_ops that takes it as a base, and by the interrupt notification —
    and a "\\n".join round trip is LOSSY for a trailing blank line:
    ['a','b',''] -> "a\\nb\\n" -> ['a','b'] drops it. Terminating is lossless.

    `before` is read through splitlines() either way, so a base may arrive in
    either form: "a\\nb" and "a\\nb\\n" are the same two lines and yield
    identical line numbering. An empty result stays "" (no lone newline) —
    both consumers derive `seeded` from the partial's truthiness."""
    lines = before.splitlines()
    for op in ops:
        if op.end_line >= op.start_line:
            del lines[op.start_line - 1 : op.end_line]
        insert_at = op.start_line - 1
        lines[insert_at:insert_at] = list(op.new_lines)
    return "".join(line + "\n" for line in lines)


def is_binary(content: bytes) -> bool:
    return b"\x00" in content[:8192]
