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


def is_binary(content: bytes) -> bool:
    return b"\x00" in content[:8192]
